import asyncio
import logging
import signal
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import Headers
from starlette.middleware.sessions import SessionMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from workflows import account, admin, api, clients, stream, web
from workflows.beans import BeansPoller
from workflows.builtin import builtin_provider
from workflows.bus import EventBus
from workflows.catalog import Catalog, Provider
from workflows.db import connect, session_factory
from workflows.jobs import InvalidEventError, JobError
from workflows.ledger import InsufficientCreditsError
from workflows.login import build_oauth
from workflows.login import router as login_router
from workflows.mcp import build_mcp
from workflows.n8n import N8nProvider
from workflows.probe import PROBE_LIMITS, ProbeError, Prober, YtdlProber
from workflows.settings import Settings, load_settings
from workflows.state import AppServices, Services
from workflows.storage import build_storage
from workflows.webhooks import WEBHOOK_TIMEOUT_SECONDS, WebhookNotifier
from workflows.worker import QueueWorker

logger = logging.getLogger(__name__)

ERROR_STATUS = {
    JobError: 409,
    InvalidEventError: 422,
    InsufficientCreditsError: 402,
    ProbeError: 502,
}
STATIC_DIR = Path(__file__).parent / "static"
MAX_BODY_BYTES = 256 * 1024
TOO_LARGE = "the request body is over 256 KB"
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src https://fonts.gstatic.com; script-src 'self'; "
        "connect-src 'self' https://s.t3ks.com; "
        "img-src 'self' data: https://s3-api.t3ks.com; media-src 'self' https://s3-api.t3ks.com; "
        "frame-ancestors 'none'; base-uri 'none'; form-action 'self' https://beans.h4ks.com"
    ),
    "Strict-Transport-Security": "max-age=31536000",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "X-Frame-Options": "DENY",
}


async def _domain_error(request: Request, error: Exception) -> JSONResponse:
    return JSONResponse({"detail": str(error)}, status_code=ERROR_STATUS[type(error)])


async def _security_headers(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    response = await call_next(request)
    response.headers.update(SECURITY_HEADERS)
    is_html = response.headers.get("content-type", "").startswith("text/html")
    if is_html and not request.url.path.startswith("/static"):
        response.headers["Cache-Control"] = "no-store"
    return response


class BodyLimit:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        declared = Headers(scope=scope).get("content-length", "")
        if declared.isdigit() and int(declared) > MAX_BODY_BYTES:
            await _too_large()(scope, receive, send)
            return
        received = 0

        async def counted_receive() -> Message:
            nonlocal received
            message = await receive()
            received += len(message.get("body", b""))
            if received > MAX_BODY_BYTES:
                raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, TOO_LARGE)
            return message

        await self.app(scope, counted_receive, send)


def _too_large() -> JSONResponse:
    return JSONResponse({"detail": TOO_LARGE}, status_code=status.HTTP_413_CONTENT_TOO_LARGE)


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


def default_providers(settings: Settings, http: httpx.AsyncClient) -> list[Provider]:
    if settings.n8n_url and settings.n8n_api_key:
        return [N8nProvider(http, settings.n8n_url, settings.n8n_api_key, settings.executor_token)]
    return [builtin_provider(http, settings.n8n_url, settings.executor_token)]


def _install_middleware(app: FastAPI, settings: Settings) -> None:
    app.add_middleware(BodyLimit)
    app.middleware("http")(_security_headers)
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret,
        https_only=settings.base_url.startswith("https://"),
    )
    for error_type in ERROR_STATUS:
        app.add_exception_handler(error_type, _domain_error)


async def _cancel_all(tasks: list[asyncio.Task[None]]) -> None:
    for task in tasks:
        task.cancel()
    for task in tasks:
        with suppress(asyncio.CancelledError):
            await task


def create_app(
    settings: Settings | None = None,
    prober: Prober | None = None,
    providers: Sequence[Provider] | None = None,
) -> FastAPI:
    settings = settings or load_settings()
    http = httpx.AsyncClient()
    probe_http = httpx.AsyncClient(limits=PROBE_LIMITS)
    engine = connect(settings.database_url)
    sessions = session_factory(engine)
    catalog = Catalog(providers if providers is not None else default_providers(settings, http))
    bus = EventBus()
    worker = QueueWorker(sessions, catalog, bus, settings)
    beans_poller = BeansPoller(sessions, http, settings) if settings.beans_token else None
    webhook_http = httpx.AsyncClient(timeout=WEBHOOK_TIMEOUT_SECONDS)
    notifier = WebhookNotifier(sessions, catalog, bus, webhook_http, settings.base_url)
    services = Services(
        settings=settings,
        sessions=sessions,
        catalog=catalog,
        bus=bus,
        prober=prober or YtdlProber(probe_http, settings.ytdl_url, settings.ytdl_api_key),
        worker=worker,
        http=http,
        oauth=build_oauth(settings),
        beans_poller=beans_poller,
        storage=build_storage(settings),
    )
    mcp = build_mcp(services)
    mcp_app = mcp.http_app(path="/", stateless_http=True)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with AsyncExitStack() as stack:
            await stack.enter_async_context(mcp_app.lifespan(app))
            await catalog.refresh()
            tasks = [
                worker.start(),
                asyncio.create_task(notifier.run(), name="webhook notifier"),
                asyncio.create_task(catalog.run(), name="job type catalog"),
            ]
            if beans_poller is not None:
                tasks.append(asyncio.create_task(beans_poller.run(), name="beans poller"))
            for task in tasks:
                task.add_done_callback(exit_when_dead)
            yield
            await _cancel_all(tasks)
            await http.aclose()
            await probe_http.aclose()
            await webhook_http.aclose()
            engine.dispose()

    app = FastAPI(title="h4ks workflows", lifespan=lifespan)
    app.state.services = services
    app.state.mcp = mcp
    _install_middleware(app, settings)
    _register_routers(app)
    app.mount("/mcp", mcp_app)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.add_api_route("/healthz", healthz)
    return app
