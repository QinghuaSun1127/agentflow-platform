"""Redis 分布式锁：用于保护敏感写操作，避免多 Agent 并发写冲突。"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import uuid4

from redis.asyncio import Redis

from app.core.config import get_redis_url


class ResourceLockedException(Exception):
    """资源已被其它 Agent 持有锁时抛出。"""


_redis_client: Redis | None = None


def _get_redis_client() -> Redis:
    """懒加载 redis.asyncio 客户端，默认连接本地 Redis。"""
    global _redis_client
    if _redis_client is None:
        redis_url = os.getenv("REDIS_URL", get_redis_url())
        _redis_client = Redis.from_url(redis_url, decode_responses=True)
    return _redis_client


@asynccontextmanager
async def acquire_lock(resource_id: str, timeout: int = 5) -> AsyncIterator[str]:
    """获取 Redis 分布式锁，并在退出上下文时安全释放。

    锁语义：
        使用 `SET resource_id token NX EX timeout`，只有 key 不存在时才能加锁；
        若 Redis 返回失败，说明其它 Agent 正在操作同一资源，立即抛出
        `ResourceLockedException`。

    Args:
        resource_id: 被保护的资源锁名，例如 `lock:order:ORD-001`。
        timeout: 锁自动过期秒数，默认 5 秒，防止 Agent 异常退出造成死锁。

    Yields:
        本次加锁生成的唯一 token，用于安全释放锁。
    """
    if timeout < 1:
        raise ValueError("timeout 必须 >= 1")

    client = _get_redis_client()
    token = uuid4().hex
    acquired = await client.set(resource_id, token, nx=True, ex=timeout)
    if not acquired:
        raise ResourceLockedException(f"资源 {resource_id} 正在被其它任务处理，请稍后重试。")

    try:
        yield token
    finally:
        # 仅当 value 仍等于自己的 token 时才删除，避免误释放其它 Agent 后续拿到的锁。
        await client.eval(
            """
            if redis.call("GET", KEYS[1]) == ARGV[1] then
                return redis.call("DEL", KEYS[1])
            end
            return 0
            """,
            1,
            resource_id,
            token,
        )
