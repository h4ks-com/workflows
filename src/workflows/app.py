import asyncio
import logging
import signal
from collections.abc import AsyncIterator
from collections.abc import Awaitable
from collections.abc import Callable
from collections.abc import Sequence
from contextlib import AsyncExitStack
from contextlib import asynccontextmanager
from contextlib import suppress
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI
from fastapi import HTTPException
from fastapi import Request
from fastapi import status
from fastapi.responses import JSONResponse
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import Headers
from starlette.middleware.sessions import SessionMiddleware
from starlette.types import ASGIApp
from starlette.types import Message
from starlette.types import Receive
from starlette.types import Scope
from starlette.types import Send

from workflows.accounts import account
from workflows.accounts.beans import BeansPoller
from workflows.accounts.beans import build_payout
from workflows.accounts.ledger import InsufficientCreditsError
from workflows.accounts.login import build_oauth
from workflows.accounts.login import router as login_router
from workflows.api import admin
from workflows.api import clients
from workflows.api import routes
from workflows.api import stream
from workflows.api.mcp import build_mcp
from workflows.db import connect
from workflows.db import session_factory
from workflows.jobtypes.catalog import Catalog
from workflows.jobtypes.catalog import Provider
from workflows.jobtypes.probe import PROBE_LIMITS
from workflows.jobtypes.probe import ProbeError
from workflows.jobtypes.probe import Prober
from workflows.jobtypes.probe import YtdlProber
from workflows.jobtypes.providers.builtin import builtin_provider
from workflows.jobtypes.providers.n8n import N8nProvider
from workflows.jobtypes.providers.services import ServiceProvider
from workflows.runs.bus import EventBus
from workflows.runs.drafts import sweep_drafts
from workflows.runs.jobs import InvalidEventError
from workflows.runs.jobs import JobError
from workflows.runs.media import build_stager
from workflows.runs.webhooks import WEBHOOK_TIMEOUT_SECONDS
from workflows.runs.webhooks import WebhookNotifier
from workflows.runs.worker import QueueWorker
from workflows.settings import Settings
from workflows.settings import load_settings
from workflows.state import AppServices
from workflows.state import Services
from workflows.storage import build_storage
from workflows.web import pages

logger = logging.getLogger(__name__)

ERROR_STATUS = {
    JobError: 409,
    InvalidEventError: 422,
    InsufficientCreditsError: 402,
    ProbeError: 502,
}
STATIC_DIR = Path(__file__).parent / "web" / "static"
MAX_BODY_BYTES = 256 * 1024
TOO_LARGE = "the request body is over 256 KB"
STATIC_HEADERS = {
    "Strict-Transport-Security": "max-age=31536000",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "X-Frame-Options": "DENY",
}


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else ""


def security_headers(settings: Settings) -> dict[str, str]:
    """Build the response headers, allowing only the media, upload and Beans hosts configured."""
    media = f"https://{settings.minio_endpoint}" if settings.minio_endpoint else ""
    directives = [
        "default-src 'self'",
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
        "font-src https://fonts.gstatic.com",
        # The 3D viewer decodes meshes with WebAssembly and fetches them from the media host.
        "script-src 'self' 'wasm-unsafe-eval'",
        f"connect-src 'self' {_origin(settings.upload_url)} {media}",
        f"img-src 'self' data: {media}",
        f"media-src 'self' {media}",
        "frame-ancestors 'none'",
        "base-uri 'none'",
        f"form-action 'self' {_origin(settings.beans_url)}",
    ]
    policy = "; ".join(directive.strip() for directive in directives)
    return {"Content-Security-Policy": policy, **STATIC_HEADERS}


async def _domain_error(request: Request, error: Exception) -> JSONResponse:
    return JSONResponse({"detail": str(error)}, status_code=ERROR_STATUS[type(error)])


def _security_middleware(
    headers: dict[str, str],
) -> Callable[[Request, Callable[[Request], Awaitable[Response]]], Awaitable[Response]]:
    async def add_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        response.headers.update(headers)
        is_html = response.headers.get("content-type", "").startswith("text/html")
        if is_html and not request.url.path.startswith("/static"):
            response.headers["Cache-Control"] = "no-store"
        return response

    return add_headers


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
    app.include_router(routes.router)
    app.include_router(account.router)
    app.include_router(clients.router)
    app.include_router(clients.pages_router)
    app.include_router(admin.router)
    app.include_router(login_router)
    app.include_router(stream.router)
    app.include_router(pages.router)


def default_providers(settings: Settings, http: httpx.AsyncClient) -> list[Provider]:
    """Pick the job type providers the settings enable."""
    services: list[Provider] = [
        ServiceProvider(http, url, settings.workflow_service_token)
        for url in settings.workflow_services
    ]
    if settings.n8n_url and settings.n8n_api_key:
        n8n = N8nProvider(http, settings.n8n_url, settings.n8n_api_key, settings.executor_token)
        return [n8n, *services]
    return [builtin_provider(http, settings.n8n_url, settings.executor_token), *services]


def _install_middleware(app: FastAPI, settings: Settings) -> None:
    app.add_middleware(BodyLimit)
    app.middleware("http")(_security_middleware(security_headers(settings)))
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
    """Build the app with its routers, middleware, background tasks and job type catalog."""
    settings = settings or load_settings()
    http = httpx.AsyncClient()
    probe_http = httpx.AsyncClient(limits=PROBE_LIMITS)
    engine = connect(settings.database_url)
    sessions = session_factory(engine)
    catalog = Catalog(providers if providers is not None else default_providers(settings, http))
    bus = EventBus()
    worker = QueueWorker(sessions, catalog, bus, settings, build_stager(settings, http))
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
        beans_payout=build_payout(http, settings),
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
                asyncio.create_task(sweep_drafts(sessions), name="draft sweeper"),
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
