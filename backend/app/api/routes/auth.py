"""Authentication and user administration."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Request
from sqlalchemy import func, select, text

from app.api.deps import AdminUser, CurrentUser, PageDep, SessionDep, SettingsDep, client_ip
from app.config import Settings
from app.db.models import User
from app.errors import AppError, Conflict, Forbidden, NotFound, Unauthenticated
from app.schemas.api import (
    ListResponse,
    LoginRequest,
    RefreshRequest,
    RegisterRequest,
    TokenResponse,
    UserOut,
    UserUpdate,
)
from app.security.auth import LoginThrottle, create_token, decode_token, hash_password, verify_password
from app.services.access import audit

router = APIRouter(tags=["auth"])

_throttle: LoginThrottle | None = None
# A valid hash to verify against when the account does not exist, so timing does not reveal it.
_DUMMY_HASH = hash_password("not-a-real-password")


def _get_throttle(settings: Settings) -> LoginThrottle:
    global _throttle
    if _throttle is None:
        _throttle = LoginThrottle(settings.login_max_attempts, settings.login_window_seconds)
    return _throttle


def reset_throttle() -> None:
    global _throttle
    _throttle = None


def _tokens(settings: Settings, user_id: uuid.UUID) -> TokenResponse:
    access, expires_in = create_token(settings, user_id=user_id, token_type="access")
    refresh, _ = create_token(settings, user_id=user_id, token_type="refresh")
    return TokenResponse(access_token=access, refresh_token=refresh, expires_in=expires_in)


@router.post("/auth/register", response_model=UserOut, status_code=201)
async def register(body: RegisterRequest, request: Request, session: SessionDep, settings: SettingsDep) -> User:
    # Serialise registrations so exactly one account can become the first administrator.
    await session.execute(text("SELECT pg_advisory_xact_lock(741001)"))
    # The installation is bootstrapped by its first administrator. Accounts created by other
    # means (the evaluation runner) do not count, so they can never occupy that role.
    admin_count = await session.scalar(select(func.count()).select_from(User).where(User.role == "admin")) or 0
    if admin_count > 0 and not settings.allow_registration:
        raise Forbidden("Registration is disabled", code="registration_disabled")
    if await session.scalar(select(User.id).where(User.email == body.email)):
        raise Conflict("An account with this email already exists", code="email_taken")
    user = User(
        email=body.email,
        full_name=body.full_name.strip(),
        password_hash=hash_password(body.password),
        role="admin" if admin_count == 0 else "developer",
    )
    session.add(user)
    await session.flush()
    await audit(session, "auth.register", actor=user, resource_type="user", resource_id=user.id,
                details={"role": user.role}, ip=client_ip(request))  # fmt: skip
    await session.commit()
    return user


@router.post("/auth/login", response_model=TokenResponse)
async def login(body: LoginRequest, request: Request, session: SessionDep, settings: SettingsDep) -> TokenResponse:
    throttle = _get_throttle(settings)
    key = f"{body.email}|{client_ip(request)}"
    throttle.check(key)
    user = await session.scalar(select(User).where(User.email == body.email))
    valid = verify_password(body.password, user.password_hash if user else _DUMMY_HASH)
    if user is None or not valid or not user.is_active:
        throttle.record_failure(key)
        await audit(session, "auth.login_failed", details={"email": body.email}, ip=client_ip(request))
        await session.commit()
        raise Unauthenticated("Invalid email or password", code="invalid_credentials")
    throttle.reset(key)
    await audit(session, "auth.login", actor=user, ip=client_ip(request))
    await session.commit()
    return _tokens(settings, user.id)


@router.post("/auth/refresh", response_model=TokenResponse)
async def refresh(body: RefreshRequest, session: SessionDep, settings: SettingsDep) -> TokenResponse:
    user_id = decode_token(settings, body.refresh_token, expected_type="refresh")
    user = await session.get(User, user_id)
    if user is None or not user.is_active:
        raise Unauthenticated("Account not found or disabled")
    return _tokens(settings, user.id)


@router.get("/auth/me", response_model=UserOut)
async def me(user: CurrentUser) -> User:
    return user


@router.get("/users", response_model=ListResponse[UserOut], tags=["users"])
async def list_users(_: AdminUser, session: SessionDep, page: PageDep) -> ListResponse[UserOut]:
    total = await session.scalar(select(func.count()).select_from(User)) or 0
    rows = await session.scalars(select(User).order_by(User.created_at).limit(page.limit).offset(page.offset))
    return ListResponse(items=[UserOut.model_validate(u) for u in rows], total=total)


@router.patch("/users/{user_id}", response_model=UserOut, tags=["users"])
async def update_user(user_id: uuid.UUID, body: UserUpdate, admin: AdminUser, session: SessionDep, request: Request) -> User:
    target = await session.get(User, user_id)
    if target is None:
        raise NotFound("User not found")
    demoting = (body.role is not None and body.role != "admin") or body.is_active is False
    if target.role == "admin" and demoting:
        other_admins = await session.scalar(
            select(func.count()).select_from(User).where(
                User.role == "admin", User.is_active.is_(True), User.id != target.id
            )
        )  # fmt: skip
        if not other_admins:
            raise AppError("At least one active administrator must remain", code="last_admin", status_code=409)
    if body.role is not None:
        target.role = body.role
    if body.is_active is not None:
        target.is_active = body.is_active
    await audit(session, "user.update", actor=admin, resource_type="user", resource_id=target.id,
                details=body.model_dump(exclude_none=True), ip=client_ip(request))  # fmt: skip
    await session.commit()
    return target
