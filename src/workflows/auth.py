import hmac
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status

from workflows.db import User
from workflows.settings import Settings
from workflows.state import AppServices, Db

SESSION_USER_KEY = "user_id"
BEARER_PREFIX = "Bearer "


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
