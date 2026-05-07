"""Password hashing and JWT helpers for local AgentFlow accounts."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from typing import Any

from app.core.config import get_jwt_secret

_JWT_ALGORITHM = "HS256"
_PASSWORD_ITERATIONS = 210_000


class AuthError(Exception):
    """Raised when token parsing or validation fails."""


def hash_password(password: str) -> str:
    """Hash a password using PBKDF2-HMAC-SHA256 with a per-user salt."""
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PASSWORD_ITERATIONS)
    return f"pbkdf2_sha256${_PASSWORD_ITERATIONS}${_b64url(salt)}${_b64url(digest)}"


def verify_password(password: str, password_hash: str) -> bool:
    """Verify a password against a stored PBKDF2 hash."""
    try:
        scheme, iterations_raw, salt_raw, digest_raw = password_hash.split("$", 3)
        if scheme != "pbkdf2_sha256":
            return False
        iterations = int(iterations_raw)
        salt = _b64url_decode(salt_raw)
        expected = _b64url_decode(digest_raw)
    except (ValueError, TypeError):
        return False

    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(actual, expected)


def create_access_token(payload: dict[str, Any], *, expires_seconds: int | None = None) -> str:
    """Create a compact HMAC-signed JWT."""
    now = int(time.time())
    ttl = expires_seconds or int(os.getenv("JWT_EXPIRES_SECONDS", "604800"))
    claims = {**payload, "iat": now, "exp": now + ttl, "token_type": "access"}
    header = {"alg": _JWT_ALGORITHM, "typ": "JWT"}
    signing_input = f"{_json_b64(header)}.{_json_b64(claims)}"
    signature = _sign(signing_input)
    return f"{signing_input}.{signature}"


def create_refresh_token(payload: dict[str, Any], *, expires_seconds: int | None = None) -> str:
    """Create refresh token JWT."""
    now = int(time.time())
    ttl = expires_seconds or int(os.getenv("JWT_REFRESH_EXPIRES_SECONDS", "2592000"))
    claims = {**payload, "iat": now, "exp": now + ttl, "token_type": "refresh"}
    header = {"alg": _JWT_ALGORITHM, "typ": "JWT"}
    signing_input = f"{_json_b64(header)}.{_json_b64(claims)}"
    signature = _sign(signing_input)
    return f"{signing_input}.{signature}"


def decode_access_token(token: str) -> dict[str, Any]:
    """Decode and validate an HMAC-signed JWT."""
    payload = _decode_token(token)
    if payload.get("token_type") not in {"access", None}:
        raise AuthError("Invalid token type")
    return payload


def decode_refresh_token(token: str) -> dict[str, Any]:
    payload = _decode_token(token)
    if payload.get("token_type") != "refresh":
        raise AuthError("Invalid refresh token")
    return payload


def _decode_token(token: str) -> dict[str, Any]:
    """Decode token and validate signature/expiry."""
    try:
        header_raw, payload_raw, signature = token.split(".", 2)
    except ValueError as exc:
        raise AuthError("Malformed token") from exc

    signing_input = f"{header_raw}.{payload_raw}"
    if not hmac.compare_digest(_sign(signing_input), signature):
        raise AuthError("Invalid token signature")

    try:
        header = json.loads(_b64url_decode(header_raw))
        payload = json.loads(_b64url_decode(payload_raw))
    except (ValueError, TypeError) as exc:
        raise AuthError("Invalid token payload") from exc

    if header.get("alg") != _JWT_ALGORITHM:
        raise AuthError("Unsupported token algorithm")
    if int(payload.get("exp", 0)) < int(time.time()):
        raise AuthError("Token expired")
    return payload


def _json_b64(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return _b64url(raw)


def _sign(value: str) -> str:
    digest = hmac.new(get_jwt_secret().encode("utf-8"), value.encode("utf-8"), hashlib.sha256).digest()
    return _b64url(digest)


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)
