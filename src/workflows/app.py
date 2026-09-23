import asyncio
import logging
import signal
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from workflows import account, admin, api, clients, stream, web
from workflows.beans import BeansPoller
from workflows.bus import EventBus
from workflows.db import connect, session_factory
from workflows.jobs import InvalidEventError, JobError
from workflows.jobtypes import ProbeError, Prober, YtdlProber, build_registry
from workflows.ledger import InsufficientCreditsError
from workflows.login import build_oauth
from workflows.login import router as login_router
from workflows.mcp import build_mcp
from workflows.settings import Settings, load_settings
from workflows.state import AppServices, Services
from workflows.worker import QueueWorker

logger = logging.getLogger(__name__)

ERROR_STATUS = {
    JobError: 409,
    InvalidEventError: 422,
    InsufficientCreditsError: 402,
    ProbeError: 502,
}
STATIC_DIR = Path(__file__).parent / "static"
CSP = (
    "default-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src https://fonts.gstatic.com; script-src 'self'; connect-src 'self'; "
    "img-src 'self' data:"
)


async def _domain_error(request: Request, error: Exception) -> JSONResponse:
    return JSONResponse({"detail": str(error)}, status_code=ERROR_STATUS[type(error)])


async def _security_headers(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    response = await call_next(request)
    response.headers["Content-Security-Policy"] = CSP
    return response


async def healthz(services: AppServices) -> JSONResponse:
    if services.worker.alive:
        return JSONResponse({"status": "ok"})
    return JSONResponse({"status": "the queue worker stopped"}, status_code=503)


def exit_when_dead(task: asyncio.Task[None]) -> None:
    if task.cancelled():
        return
    logger.critical("background task %s died", task.get_name(), exc_info=task.exception())
    signal.raise_signal(signal.SIGTERM)


def _register_routers(app: FastAPI) -> None:
    app.include_router(api.router)
    app.include_router(account.router)
    app.include_router(clients.router)
    app.include_router(clients.pages_router)
    app.include_router(admin.router)
    app.include_router(login_router)
    app.include_router(stream.router)
    app.include_router(web.router)


def create_app(settings: Settings | None = None, prober: Prober | None = None) -> FastAPI:
    settings = settings or load_settings()
    http = httpx.AsyncClient()
    engine = connect(settings.database_url)
    sessions = session_factory(engine)
    registry = build_registry(settings.executor_urls)
    bus = EventBus()
    worker = QueueWorker(sessions, registry, bus, http, settings)
    beans_poller = BeansPoller(sessions, http, settings) if settings.beans_token else None
    services = Services(
        settings=settings,
        sessions=sessions,
        registry=registry,
        bus=bus,
        prober=prober or YtdlProber(http, settings.ytdl_url, settings.ytdl_api_key),
        worker=worker,
        http=http,
        oauth=build_oauth(settings),
        beans_poller=beans_poller,
    )
    mcp = build_mcp(services)
    mcp_app = mcp.http_app(path="/", stateless_http=True)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with AsyncExitStack() as stack:
            await stack.enter_async_context(mcp_app.lifespan(app))
            tasks = [worker.start()]
            if beans_poller is not None:
                tasks.append(asyncio.create_task(beans_poller.run(), name="beans poller"))
            for task in tasks:
                task.add_done_callback(exit_when_dead)
            yield
            for task in tasks:
                task.cancel()
            for task in tasks:
                with suppress(asyncio.CancelledError):
                    await task
            await http.aclose()
            engine.dispose()

    app = FastAPI(title="h4ks workflows", lifespan=lifespan)
    app.state.services = services
    app.state.mcp = mcp
    app.middleware("http")(_security_headers)
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret,
        https_only=settings.base_url.startswith("https://"),
    )
    for error_type in ERROR_STATUS:
        app.add_exception_handler(error_type, _domain_error)
    _register_routers(app)
    app.mount("/mcp", mcp_app)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.add_api_route("/healthz", healthz)
    return app
