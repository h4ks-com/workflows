import hmac
import secrets
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status

from workflows.db import User
from workflows.settings import Settings
from workflows.state import AppServices, Db

SESSION_USER_KEY = "user_id"
CSRF_SESSION_KEY = "csrf_token"
BEARER_PREFIX = "Bearer "
FETCH_HEADER = "x-requested-with"
FETCH_HEADER_VALUE = "fetch"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def bearer_token(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    return header.removeprefix(BEARER_PREFIX) if header.startswith(BEARER_PREFIX) else None


def is_service(request: Request, settings: Settings) -> bool:
    token = bearer_token(request)
    if not token or not settings.service_token:
        return False
    return hmac.compare_digest(token.encode(), settings.service_token.encode())


def is_admin(user: User, settings: Settings) -> bool:
    return user.username in settings.admin_users


async def current_user(request: Request, session: Db) -> User | None:
    user_id = request.session.get(SESSION_USER_KEY)
    return session.get(User, user_id) if isinstance(user_id, int) else None


CurrentUser = Annotated[User | None, Depends(current_user)]


async def require_user(user: CurrentUser) -> User:
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "log in first")
    return user


LoggedInUser = Annotated[User, Depends(require_user)]


async def require_admin(user: LoggedInUser, services: AppServices) -> User:
    if not is_admin(user, services.settings):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "admins only")
    return user


async def require_service(request: Request, services: AppServices) -> None:
    if not is_service(request, services.settings):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "service token required")


async def require_fetch_header(request: Request) -> None:
    if request.method in SAFE_METHODS or bearer_token(request) is not None:
        return
    if SESSION_USER_KEY not in request.session:
        return
    if request.headers.get(FETCH_HEADER) != FETCH_HEADER_VALUE:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "send the X-Requested-With: fetch header")


def csrf_token(request: Request) -> str:
    token = request.session.get(CSRF_SESSION_KEY)
    if not isinstance(token, str):
        token = secrets.token_urlsafe(32)
        request.session[CSRF_SESSION_KEY] = token
    return token


def verify_csrf(request: Request, token: str) -> None:
    expected = request.session.get(CSRF_SESSION_KEY)
    if not isinstance(expected, str) or not hmac.compare_digest(expected.encode(), token.encode()):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "invalid or missing csrf token")
