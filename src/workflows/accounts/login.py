from typing import Annotated
from typing import cast
from urllib.parse import urlsplit

from authlib.integrations.starlette_client import OAuth
from fastapi import APIRouter
from fastapi import HTTPException
from fastapi import Query
from fastapi import Request
from fastapi import status
from fastapi.responses import RedirectResponse

from workflows.accounts.account import get_or_create_user
from workflows.accounts.auth import SESSION_USER_KEY
from workflows.settings import Settings
from workflows.state import AppServices
from workflows.state import Db

LOGTO_CLIENT_NAME = "logto"
USERNAME_CLAIM = "username"
NEXT_SESSION_KEY = "login_next"
DEFAULT_NEXT = "/"

router = APIRouter()


def build_oauth(settings: Settings) -> OAuth | None:
    if not settings.logto_endpoint:
        return None
    oauth = OAuth()
    oauth.register(
        name=LOGTO_CLIENT_NAME,
        server_metadata_url=f"{settings.logto_endpoint}/oidc/.well-known/openid-configuration",
        client_id=settings.logto_app_id,
        client_secret=settings.logto_app_secret,
        client_kwargs={"scope": "openid profile"},
    )
    return oauth


def safe_next(candidate: str | None) -> str:
    if not candidate or not candidate.startswith("/") or candidate.startswith("//"):
        return DEFAULT_NEXT
    if "\\" in candidate:
        return DEFAULT_NEXT
    parsed = urlsplit(candidate)
    return candidate if not parsed.scheme and not parsed.netloc else DEFAULT_NEXT


def _dev_login(request: Request, session: Db, username: str, next_path: str) -> RedirectResponse:
    user = get_or_create_user(session, logto_sub=f"dev:{username}", username=username)
    session.commit()
    start_session(request, user.id)
    return RedirectResponse(next_path)


def start_session(request: Request, user_id: int) -> None:
    request.session.clear()
    request.session[SESSION_USER_KEY] = user_id


@router.get("/login")
async def login(
    request: Request,
    services: AppServices,
    session: Db,
    next: str | None = None,
    as_: Annotated[str | None, Query(alias="as")] = None,
) -> RedirectResponse:
    next_path = safe_next(next)
    if services.settings.dev_login and as_ is not None:
        return _dev_login(request, session, as_, next_path)
    if services.oauth is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "login is not configured")
    request.session[NEXT_SESSION_KEY] = next_path
    redirect_uri = f"{services.settings.base_url}/auth/callback"
    redirect = await services.oauth.logto.authorize_redirect(request, redirect_uri)
    return cast(RedirectResponse, redirect)


@router.get("/auth/callback")
async def auth_callback(request: Request, services: AppServices, session: Db) -> RedirectResponse:
    if services.oauth is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "login is not configured")
    token = await services.oauth.logto.authorize_access_token(request)
    userinfo = token.get("userinfo") or {}
    sub = userinfo.get("sub")
    username = userinfo.get(USERNAME_CLAIM)
    if not isinstance(sub, str) or not isinstance(username, str):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "logto did not return a username")
    user = get_or_create_user(session, logto_sub=sub, username=username)
    session.commit()
    next_path = request.session.get(NEXT_SESSION_KEY, DEFAULT_NEXT)
    start_session(request, user.id)
    return RedirectResponse(next_path)


@router.get("/logout")
async def logout(request: Request) -> RedirectResponse:
    request.session.clear()
    return RedirectResponse(DEFAULT_NEXT)
