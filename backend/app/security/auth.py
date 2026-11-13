"""Password hashing, JWT handling and credential encryption."""

from __future__ import annotations

import base64
import hashlib
import time
import uuid
from collections import defaultdict, deque
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import Argon2Error, InvalidHashError
from cryptography.fernet import Fernet, InvalidToken

from app.config import Settings
from app.errors import AppError, RateLimited, Unauthenticated

_hasher = PasswordHasher()
_ALGORITHM = "HS256"
TokenType = Literal["access", "refresh"]


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (Argon2Error, InvalidHashError):
        return False


def create_token(settings: Settings, *, user_id: uuid.UUID, token_type: TokenType) -> tuple[str, int]:
    lifetime = (
        timedelta(minutes=settings.access_token_minutes)
        if token_type == "access"  # noqa: S105
        else timedelta(days=settings.refresh_token_days)
    )
    now = datetime.now(UTC)
    payload = {
        "sub": str(user_id),
        "typ": token_type,
        "iat": int(now.timestamp()),
        "exp": int((now + lifetime).timestamp()),
        "jti": uuid.uuid4().hex,
    }
    token = jwt.encode(payload, settings.jwt_secret.get_secret_value(), algorithm=_ALGORITHM)
    return token, int(lifetime.total_seconds())


def decode_token(settings: Settings, token: str, *, expected_type: TokenType) -> uuid.UUID:
    try:
        payload: dict[str, Any] = jwt.decode(
            token,
            settings.jwt_secret.get_secret_value(),
            algorithms=[_ALGORITHM],
            options={"require": ["exp", "sub", "typ"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise Unauthenticated("Token has expired", code="token_expired") from exc
    except jwt.PyJWTError as exc:
        raise Unauthenticated("Invalid token") from exc
    if payload.get("typ") != expected_type:
        raise Unauthenticated("Wrong token type")
    try:
        return uuid.UUID(payload["sub"])
    except ValueError as exc:
        raise Unauthenticated("Invalid token subject") from exc


# --- credential encryption -------------------------------------------------------------


def _fernet(settings: Settings) -> Fernet:
    if settings.credential_encryption_key:
        return Fernet(settings.credential_encryption_key.get_secret_value().encode())
    # Development convenience only; production refuses to start without an explicit key.
    derived = hashlib.sha256(b"asea-credential:" + settings.jwt_secret.get_secret_value().encode()).digest()
    return Fernet(base64.urlsafe_b64encode(derived))


def encrypt_secret(settings: Settings, plaintext: str) -> str:
    return _fernet(settings).encrypt(plaintext.encode()).decode()


def decrypt_secret(settings: Settings, ciphertext: str) -> str:
    try:
        return _fernet(settings).decrypt(ciphertext.encode()).decode()
    except InvalidToken as exc:
        raise AppError(
            "Stored credential cannot be decrypted; it was encrypted with a different key. Save it again.",
            code="credential_unreadable",
            status_code=409,
        ) from exc


# --- login throttling ------------------------------------------------------------------


class LoginThrottle:
    """In-process sliding-window limiter for failed logins (per email + address)."""

    def __init__(self, max_attempts: int, window_seconds: int) -> None:
        self.max_attempts = max_attempts
        self.window = window_seconds
        self._failures: dict[str, deque[float]] = defaultdict(deque)

    def _prune(self, key: str, now: float) -> deque[float]:
        q = self._failures[key]
        while q and now - q[0] > self.window:
            q.popleft()
        return q

    def check(self, key: str) -> None:
        now = time.monotonic()
        q = self._prune(key, now)
        if len(q) >= self.max_attempts:
            retry = int(self.window - (now - q[0])) + 1
            raise RateLimited("Too many failed login attempts. Try again later.", details={"retry_after_seconds": retry})

    def record_failure(self, key: str) -> None:
        self._failures[key].append(time.monotonic())

    def reset(self, key: str) -> None:
        self._failures.pop(key, None)
