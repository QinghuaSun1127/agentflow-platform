"""基于 Redis 的 LangGraph 异步 Checkpoint 存储（短期记忆，写入时 key TTL 24h）。"""

from __future__ import annotations

import base64
import json
import random
import time
from collections.abc import AsyncIterator, Sequence
from typing import Any, cast

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    WRITES_IDX_MAP,
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    SerializerProtocol,
    get_checkpoint_id,
    get_checkpoint_metadata,
)
from redis.asyncio import Redis

# LangGraph 无独立 AsyncBaseCheckpointSaver；实现 BaseCheckpointSaver 的异步方法即可。
TTL_SECONDS_DEFAULT = 24 * 3600


def _ns_token(checkpoint_ns: str) -> str:
    """避免空 checkpoint_ns 导致 key 片段歧义。"""
    return checkpoint_ns if checkpoint_ns != "" else "__root__"


class RedisAsyncCheckpointSaver(BaseCheckpointSaver[str]):
    """redis.asyncio 客户端持久化 checkpoint；aput / aput_writes 后对相关 key 设置 24h TTL。"""

    def __init__(
        self,
        client: Redis,
        *,
        ttl_seconds: int = TTL_SECONDS_DEFAULT,
        key_prefix: str = "agentflow:lg",
        serde: SerializerProtocol | None = None,
    ) -> None:
        super().__init__(serde=serde)
        self._redis = client
        self._ttl = ttl_seconds
        self._pfx = key_prefix.rstrip(":")

    def _ck_key(self, thread_id: str, checkpoint_ns: str, checkpoint_id: str) -> str:
        return f"{self._pfx}:ck:{thread_id}:{_ns_token(checkpoint_ns)}:{checkpoint_id}"

    def _ix_key(self, thread_id: str, checkpoint_ns: str) -> str:
        return f"{self._pfx}:ix:{thread_id}:{_ns_token(checkpoint_ns)}"

    def _wl_key(self, thread_id: str, checkpoint_ns: str, checkpoint_id: str) -> str:
        return f"{self._pfx}:wl:{thread_id}:{_ns_token(checkpoint_ns)}:{checkpoint_id}"

    async def setup(self) -> None:
        """探测 Redis 可用性。"""
        await self._redis.ping()

    async def _expire_many(self, *keys: str) -> None:
        keys = tuple(k for k in keys if k)
        if not keys:
            return
        async with self._redis.pipeline(transaction=False) as pipe:
            for k in keys:
                pipe.expire(k, self._ttl)
            await pipe.execute()

    def get_next_version(self, current: str | None, channel: None) -> str:  # noqa: ARG002
        """与 SqliteSaver 一致的字符串单调版本，满足 channel version 递增约定。"""
        if current is None:
            current_v = 0
        elif isinstance(current, int):
            current_v = current
        else:
            current_v = int(str(current).split(".")[0])
        next_v = current_v + 1
        next_h = random.random()
        return f"{next_v:032}.{next_h:016}"

    async def _tuple_for(
        self,
        thread_id: str,
        checkpoint_ns: str,
        checkpoint_id: str,
        *,
        resolved_config: RunnableConfig | None,
    ) -> CheckpointTuple | None:
        data = await self._redis.hgetall(self._ck_key(thread_id, checkpoint_ns, checkpoint_id))
        if not data:
            return None

        parent_raw = data.get(b"parent")
        type_raw = data.get(b"type")
        ck_raw = data.get(b"checkpoint")
        meta_raw = data.get(b"metadata")
        if type_raw is None or ck_raw is None:
            return None

        if isinstance(parent_raw, bytes | bytearray):
            parent_checkpoint_id = parent_raw.decode("utf-8", "ignore") or None
        else:
            parent_checkpoint_id = str(parent_raw or "") or None

        type_s = type_raw.decode("utf-8", "ignore") if isinstance(type_raw, (bytes, bytearray)) else str(type_raw)
        if isinstance(meta_raw, bytes | bytearray):
            meta_bytes = meta_raw
        else:
            meta_bytes = str(meta_raw).encode("utf-8") if meta_raw else b"{}"

        wl = self._wl_key(thread_id, checkpoint_ns, checkpoint_id)
        raw_writes = await self._redis.lrange(wl, 0, -1)
        pending_writes: list[tuple[str, str, Any]] = []
        for row in raw_writes:
            rec = json.loads(row.decode("utf-8") if isinstance(row, (bytes, bytearray)) else row)
            task_id, _widx, channel, t_s, b64 = rec
            blob = base64.b64decode(b64)
            pending_writes.append((task_id, channel, self.serde.loads_typed((t_s, blob))))

        out_cfg: RunnableConfig = resolved_config or {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": checkpoint_id,
            }
        }

        return CheckpointTuple(
            out_cfg,
            self.serde.loads_typed((type_s, ck_raw if isinstance(ck_raw, (bytes, bytearray)) else bytes(ck_raw))),
            cast(CheckpointMetadata, json.loads(meta_bytes.decode("utf-8", "ignore") or "{}")),
            (
                {
                    "configurable": {
                        "thread_id": thread_id,
                        "checkpoint_ns": checkpoint_ns,
                        "checkpoint_id": parent_checkpoint_id,
                    }
                }
                if parent_checkpoint_id
                else None
            ),
            pending_writes,
        )

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        thread_id = str(config["configurable"]["thread_id"])
        checkpoint_ns = config["configurable"].get("checkpoint_ns", "")

        if checkpoint_id := get_checkpoint_id(config):
            return await self._tuple_for(
                thread_id,
                checkpoint_ns,
                checkpoint_id,
                resolved_config=config,
            )

        ix = self._ix_key(thread_id, checkpoint_ns)
        members = await self._redis.zrevrange(ix, 0, 0)
        if not members:
            return None
        mid = members[0]
        checkpoint_id = mid.decode("utf-8") if isinstance(mid, (bytes, bytearray)) else str(mid)
        return await self._tuple_for(
            thread_id,
            checkpoint_ns,
            checkpoint_id,
            resolved_config=None,
        )

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        if config is None:
            async for key in self._redis.scan_iter(match=f"{self._pfx}:ix:*", count=64):
                key_s = key.decode("utf-8", "ignore") if isinstance(key, (bytes, bytearray)) else str(key)
                parts = key_s.split(":")
                if len(parts) < 5:
                    continue
                thread_id, ns_tok = parts[3], parts[4]
                checkpoint_ns = "" if ns_tok == "__root__" else ns_tok
                async for tup in self._iter_thread_ns(
                    thread_id, checkpoint_ns, filter, before, limit
                ):
                    yield tup
            return

        thread_id = str(config["configurable"]["thread_id"])
        checkpoint_ns = config["configurable"].get("checkpoint_ns", "")
        if get_checkpoint_id(config):
            tup = await self.aget_tuple(config)
            if tup:
                yield tup
            return

        async for tup in self._iter_thread_ns(
            thread_id, checkpoint_ns, filter, before, limit
        ):
            yield tup

    async def _iter_thread_ns(
        self,
        thread_id: str,
        checkpoint_ns: str,
        filter: dict[str, Any] | None,
        before: RunnableConfig | None,
        limit: int | None,
    ) -> AsyncIterator[CheckpointTuple]:
        ix = self._ix_key(thread_id, checkpoint_ns)
        before_id = get_checkpoint_id(before) if before else None
        ids = await self._redis.zrevrange(ix, 0, -1)
        n = 0
        for mid in ids:
            cid = mid.decode("utf-8") if isinstance(mid, (bytes, bytearray)) else str(mid)
            if before_id and cid >= before_id:
                continue
            data = await self._redis.hgetall(self._ck_key(thread_id, checkpoint_ns, cid))
            if not data:
                continue
            meta_raw = data.get(b"metadata") or data.get("metadata") or b"{}"
            meta_text = meta_raw.decode("utf-8", "ignore") if isinstance(meta_raw, bytes | bytearray) else meta_raw
            metadata = cast(CheckpointMetadata, json.loads(meta_text))
            if filter and not all(metadata.get(k) == v for k, v in filter.items()):
                continue
            cfg: RunnableConfig = {
                "configurable": {
                    "thread_id": thread_id,
                    "checkpoint_ns": checkpoint_ns,
                    "checkpoint_id": cid,
                }
            }
            tup = await self._tuple_for(thread_id, checkpoint_ns, cid, resolved_config=cfg)
            if tup:
                yield tup
                n += 1
                if limit is not None and n >= limit:
                    break

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        # new_versions 由框架传入；本 MVP 将完整 checkpoint 序列化入 Redis，与 SqliteSaver 存整条一致
        _ = new_versions
        thread_id = str(config["configurable"]["thread_id"])
        checkpoint_ns = config["configurable"].get("checkpoint_ns", "")
        new_id = checkpoint["id"]
        parent_id = config["configurable"].get("checkpoint_id")

        type_, blob = self.serde.dumps_typed(checkpoint)
        meta_bytes = json.dumps(
            get_checkpoint_metadata(config, metadata),
            ensure_ascii=False,
        ).encode("utf-8", "ignore")

        ck = self._ck_key(thread_id, checkpoint_ns, new_id)
        ix = self._ix_key(thread_id, checkpoint_ns)
        wl = self._wl_key(thread_id, checkpoint_ns, new_id)

        await self._redis.delete(wl)
        mapping = {
            b"parent": (parent_id or "").encode("utf-8"),
            b"type": type_.encode("utf-8"),
            b"checkpoint": blob if isinstance(blob, (bytes, bytearray)) else bytes(blob),
            b"metadata": meta_bytes,
        }
        await self._redis.hset(ck, mapping=mapping)
        await self._redis.zadd(ix, {new_id: time.time()})
        await self._expire_many(ck, ix, wl)

        return {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": new_id,
            }
        }

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        del task_path
        thread_id = str(config["configurable"]["thread_id"])
        checkpoint_ns = config["configurable"].get("checkpoint_ns", "")
        checkpoint_id = str(config["configurable"]["checkpoint_id"])
        ck = self._ck_key(thread_id, checkpoint_ns, checkpoint_id)
        wl = self._wl_key(thread_id, checkpoint_ns, checkpoint_id)
        ix = self._ix_key(thread_id, checkpoint_ns)

        for idx, (channel, value) in enumerate(writes):
            widx = WRITES_IDX_MAP.get(channel, idx)
            t_s, payload = self.serde.dumps_typed(value)
            rec = json.dumps(
                [task_id, widx, channel, t_s, base64.b64encode(payload).decode("ascii")],
                ensure_ascii=False,
            )
            await self._redis.rpush(wl, rec.encode("utf-8"))
        await self._expire_many(ck, wl, ix)

    async def adelete_thread(self, thread_id: str) -> None:
        t = str(thread_id)
        async for key in self._redis.scan_iter(match=f"{self._pfx}:*:{t}:*", count=128):
            await self._redis.delete(key)


async def create_redis_checkpointer(
    url: str = "redis://localhost:6379/0",
    *,
    ttl_seconds: int = TTL_SECONDS_DEFAULT,
) -> RedisAsyncCheckpointSaver:
    """工厂：创建异步 Redis 客户端并包装为 CheckpointSaver。"""
    client = Redis.from_url(url, decode_responses=False)
    saver = RedisAsyncCheckpointSaver(client, ttl_seconds=ttl_seconds)
    await saver.setup()
    return saver
