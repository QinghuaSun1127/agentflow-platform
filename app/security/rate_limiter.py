"""Redis-backed token bucket rate limiting for API calls."""

from __future__ import annotations

import os
import time

from redis.asyncio import Redis

from app.core.config import get_redis_url

_redis_client: Redis | None = None


def _get_redis_client() -> Redis:
    global _redis_client
    if _redis_client is None:
        _redis_client = Redis.from_url(get_redis_url(), decode_responses=True)
    return _redis_client


class RateLimitExceeded(Exception):
    """Raised when a caller exceeds the configured request budget."""


async def enforce_rate_limit(identity: str, *, limit: int | None = None, window_seconds: int | None = None) -> None:
    """Enforce a fixed-window limit in Redis.

    Defaults are intentionally modest for LLM cost protection and can be tuned through
    `RATE_LIMIT_REQUESTS` and `RATE_LIMIT_WINDOW_SECONDS`.
    """

    request_limit = limit or int(os.getenv("RATE_LIMIT_REQUESTS", "30"))
    window = window_seconds or int(os.getenv("RATE_LIMIT_WINDOW_SECONDS", "60"))
    if request_limit < 1 or window < 1:
        return

    bucket = int(time.time() // window)
    key = f"agentflow:rate:{identity}:{bucket}"
    client = _get_redis_client()
    count = await client.incr(key)
    if count == 1:
        await client.expire(key, window)
    if count > request_limit:
        raise RateLimitExceeded(f"rate limit exceeded for {identity}")
