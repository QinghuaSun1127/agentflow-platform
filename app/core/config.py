"""Centralized runtime configuration for AgentFlow."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
load_dotenv(ROOT_DIR / ".env")


def _split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


@lru_cache(maxsize=1)
def get_api_keys() -> set[str]:
    """Return configured API keys. An empty set disables auth for local development."""

    return set(_split_csv(os.getenv("API_KEYS")))


@lru_cache(maxsize=1)
def get_cors_origins() -> list[str]:
    """Return allowed browser origins for CORS."""

    origins = _split_csv(os.getenv("CORS_ALLOW_ORIGINS"))
    return origins or ["http://localhost:8501", "http://127.0.0.1:8501"]


def get_database_url() -> str:
    return os.getenv("DATABASE_URL", "postgresql://admin:password123@localhost:5432/agentflow_db")


def get_redis_url() -> str:
    return os.getenv("REDIS_URL", "redis://localhost:6379/0")


def get_frontend_api_base_url() -> str:
    return os.getenv("AGENTFLOW_API_BASE_URL", "http://127.0.0.1:8000").rstrip("/")


def get_jwt_secret() -> str:
    return os.getenv("JWT_SECRET", "agentflow-local-dev-secret-change-me")


def get_app_env() -> str:
    return os.getenv("APP_ENV", "development").strip().lower()


def validate_production_security() -> None:
    """Fail fast on insecure defaults in production."""
    if get_app_env() not in {"prod", "production"}:
        return

    jwt_secret = get_jwt_secret()
    if len(jwt_secret) < 32 or "change-me" in jwt_secret.lower() or "local-dev" in jwt_secret.lower():
        raise RuntimeError("生产环境 JWT_SECRET 不安全，请设置 32+ 位强随机字符串。")

    db_url = get_database_url().lower()
    if "password123" in db_url:
        raise RuntimeError("生产环境 DATABASE_URL 仍使用默认弱密码。")

    redis_url = get_redis_url().lower()
    if "redis://:" not in redis_url:
        raise RuntimeError("生产环境 REDIS_URL 必须包含密码。")

    cors = get_cors_origins()
    if "*" in cors or any("localhost" in origin or "127.0.0.1" in origin for origin in cors):
        raise RuntimeError("生产环境 CORS_ALLOW_ORIGINS 不能包含 * / localhost。")
