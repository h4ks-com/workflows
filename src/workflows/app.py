import asyncio
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager, suppress

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.middleware.sessions import SessionMiddleware

from workflows import api, stream
from workflows.bus import EventBus
from workflows.db import connect, session_factory
from workflows.jobs import InvalidEventError, JobError
from workflows.jobtypes import ProbeError, Prober, YtdlProber, build_registry
from workflows.ledger import InsufficientCreditsError
from workflows.mcp import build_mcp
from workflows.settings import Settings, load_settings
from workflows.state import Services
from workflows.worker import QueueWorker

ERROR_STATUS = {
    JobError: 409,
    InvalidEventError: 422,
    InsufficientCreditsError: 402,
    ProbeError: 502,
}


async def _domain_error(request: Request, error: Exception) -> JSONResponse:
    return JSONResponse({"detail": str(error)}, status_code=ERROR_STATUS[type(error)])


async def healthz() -> dict[str, str]:
    return {"status": "ok"}


def create_app(settings: Settings | None = None, prober: Prober | None = None) -> FastAPI:
    settings = settings or load_settings()
    http = httpx.AsyncClient()
    engine = connect(settings.database_url)
    sessions = session_factory(engine)
    registry = build_registry(settings.executor_urls)
    bus = EventBus()
    worker = QueueWorker(sessions, registry, bus, http, settings)
    services = Services(
        settings=settings,
        sessions=sessions,
        registry=registry,
        bus=bus,
        prober=prober or YtdlProber(http, settings.ytdl_url, settings.ytdl_api_key),
        worker=worker,
    )
    mcp = build_mcp(services)
    mcp_app = mcp.http_app(path="/", stateless_http=True)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with AsyncExitStack() as stack:
            await stack.enter_async_context(mcp_app.lifespan(app))
            task = asyncio.create_task(worker.run())
            yield
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            await http.aclose()
            engine.dispose()

    app = FastAPI(title="h4ks workflows", lifespan=lifespan)
    app.state.services = services
    app.state.mcp = mcp
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret,
        https_only=settings.base_url.startswith("https://"),
    )
    for error_type in ERROR_STATUS:
        app.add_exception_handler(error_type, _domain_error)
    app.include_router(api.router)
    app.include_router(stream.router)
    app.mount("/mcp", mcp_app)
    app.add_api_route("/healthz", healthz)
    return app
