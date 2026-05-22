from collections.abc import Callable
from typing import Protocol
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.security import (
    create_session_token,
    hash_password,
    verify_password,
    verify_session_token,
)
from app.db.session import get_db_session
from app.models import User
from app.schemas.auth import LoginRequest, LogoutResponse, RegisterRequest, UserResponse
from app.services.rate_limit import RedisLoginRateLimiter
from app.services.users import DuplicateUserEmailError, SqlAlchemyUserStore

router = APIRouter(prefix="/auth", tags=["auth"])

INVALID_CREDENTIALS_MESSAGE = "Invalid email or password"
DB_SESSION_DEP = Depends(get_db_session)
SETTINGS_DEP = Depends(get_settings)


class UserStore(Protocol):
    async def get_by_email(self, email: str) -> User | None: ...

    async def get_by_id(self, user_id: UUID) -> User | None: ...

    async def register_user(self, email: str, name: str | None, password_hash: str) -> User: ...


class LoginRateLimiter(Protocol):
    async def check(self, email: str, request: Request) -> None: ...

    async def reset(self, email: str, request: Request) -> None: ...


async def get_user_store(session: AsyncSession = DB_SESSION_DEP) -> UserStore:
    return SqlAlchemyUserStore(session)


def get_login_rate_limiter() -> LoginRateLimiter:
    return RedisLoginRateLimiter()


USER_STORE_DEP = Depends(get_user_store)
RATE_LIMITER_DEP = Depends(get_login_rate_limiter)


def set_auth_cookie(response: Response, user: User, settings: Settings) -> None:
    token = create_session_token(
        user_id=str(user.id),
        secret_key=settings.auth_secret_key,
        ttl_seconds=settings.auth_session_ttl_seconds,
    )
    response.set_cookie(
        key=settings.auth_cookie_name,
        value=token,
        max_age=settings.auth_session_ttl_seconds,
        httponly=True,
        secure=settings.auth_cookie_secure,
        samesite="lax",
        path="/",
    )


def clear_auth_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        key=settings.auth_cookie_name,
        httponly=True,
        secure=settings.auth_cookie_secure,
        samesite="lax",
        path="/",
    )


async def get_current_user(
    request: Request,
    store: UserStore = USER_STORE_DEP,
    settings: Settings = SETTINGS_DEP,
) -> User:
    token = request.cookies.get(settings.auth_cookie_name)
    if token is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")

    payload = verify_session_token(token, settings.auth_secret_key)
    if payload is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")

    try:
        user_id = UUID(str(payload["sub"]))
    except (KeyError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        ) from None

    user = await store.get_by_id(user_id)
    if user is None or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")

    return user


CURRENT_USER_DEP = Depends(get_current_user)


def require_role(*allowed_roles: str) -> Callable[..., User]:
    async def role_guard(user: User = CURRENT_USER_DEP) -> User:
        if user.role not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Insufficient permissions",
            )
        return user

    return role_guard


@router.post("/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def register(
    payload: RegisterRequest,
    response: Response,
    store: UserStore = USER_STORE_DEP,
    settings: Settings = SETTINGS_DEP,
) -> User:
    try:
        user = await store.register_user(
            email=payload.email,
            name=payload.name,
            password_hash=hash_password(payload.password),
        )
    except DuplicateUserEmailError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Unable to register with those credentials",
        ) from None

    set_auth_cookie(response, user, settings)
    return user


@router.post("/login", response_model=UserResponse)
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    store: UserStore = USER_STORE_DEP,
    rate_limiter: LoginRateLimiter = RATE_LIMITER_DEP,
    settings: Settings = SETTINGS_DEP,
) -> User:
    await rate_limiter.check(payload.email, request)
    user = await store.get_by_email(payload.email)

    password_is_valid = user is not None and verify_password(payload.password, user.password_hash)
    if user is None or not user.is_active or not password_is_valid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=INVALID_CREDENTIALS_MESSAGE,
        )

    await rate_limiter.reset(payload.email, request)
    set_auth_cookie(response, user, settings)
    return user


@router.post("/logout", response_model=LogoutResponse)
async def logout(
    response: Response,
    settings: Settings = SETTINGS_DEP,
) -> LogoutResponse:
    clear_auth_cookie(response, settings)
    return LogoutResponse(ok=True)


@router.get("/me", response_model=UserResponse)
async def me(current_user: User = CURRENT_USER_DEP) -> User:
    return current_user
