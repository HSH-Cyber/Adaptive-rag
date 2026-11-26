"""Short-TTL cache for static policy retrieval only.

Never cache order/user-specific tool results. Cache key is query + top_k.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time

from app.schemas.retrieval import RetrievalScope, RetrievedChunk

logger = logging.getLogger(__name__)


# 缓存规则：项目只缓存静态知识检索，不缓存订单类查询结果。这体现了一个很重要的工程思想：不是所有数据都适合缓存，尤其是实时业务数据
def _looks_like_order_query(query: str) -> bool:
    compact = query.upper().replace(" ", "")
    return "ORD" in compact or "订单" in query


def cache_key(query: str, top_k: int, *, scope: RetrievalScope | None = None) -> str:
    scoped = (
        json.dumps(
            {
                "snapshot": scope.snapshot_id,
                "documents": sorted(scope.document_ids),
                "evidence": sorted(
                    [ref.model_dump() for ref in scope.evidence],
                    key=lambda r: (r["document_id"], r["policy_version"], r["source_digest"]),
                )
                if scope.evidence is not None
                else None,
                "versions": sorted(scope.policy_versions)
                if scope.policy_versions is not None
                else None,
            },
            sort_keys=True,
        )
        if scope
        else ""
    )
    digest = hashlib.sha256(f"{query}|{top_k}|{scoped}".encode()).hexdigest()[:32]
    return f"ret:{digest}"


class RetrievalCache:
    async def get(
        self, query: str, top_k: int, *, scope: RetrievalScope | None = None
    ) -> list[RetrievedChunk] | None:
        return None

    async def set(
        self,
        query: str,
        top_k: int,
        chunks: list[RetrievedChunk],
        *,
        scope: RetrievalScope | None = None,
    ) -> None:
        return None


# 缓存保存在内存中。
class MemoryRetrievalCache(RetrievalCache):
    def __init__(self, *, ttl_seconds: int) -> None:
        self._ttl = ttl_seconds
        self._store: dict[str, tuple[float, list[RetrievedChunk]]] = {}

    async def get(
        self, query: str, top_k: int, *, scope: RetrievalScope | None = None
    ) -> list[RetrievedChunk] | None:
        if _looks_like_order_query(query):
            return None
        item = self._store.get(cache_key(query, top_k, scope=scope))
        if item is None:
            return None
        expires_at, chunks = item
        if time.monotonic() > expires_at:
            self._store.pop(cache_key(query, top_k, scope=scope), None)
            return None
        return chunks

    async def set(
        self,
        query: str,
        top_k: int,
        chunks: list[RetrievedChunk],
        *,
        scope: RetrievalScope | None = None,
    ) -> None:
        if _looks_like_order_query(query):
            return
        self._store[cache_key(query, top_k, scope=scope)] = (time.monotonic() + self._ttl, chunks)


# 保存在 Redis 中
class RedisRetrievalCache(RetrievalCache):
    def __init__(self, redis, *, ttl_seconds: int) -> None:
        self._redis = redis
        self._ttl = ttl_seconds

    async def get(
        self, query: str, top_k: int, *, scope: RetrievalScope | None = None
    ) -> list[RetrievedChunk] | None:
        if _looks_like_order_query(query):
            return None
        try:
            raw = await self._redis.get(cache_key(query, top_k, scope=scope))
        except Exception:
            logger.warning("retrieval_cache_redis_get_failed")
            return None
        if not raw:
            return None
        payload = json.loads(raw)
        return [RetrievedChunk.model_validate(item) for item in payload]

    async def set(
        self,
        query: str,
        top_k: int,
        chunks: list[RetrievedChunk],
        *,
        scope: RetrievalScope | None = None,
    ) -> None:
        if _looks_like_order_query(query):
            return
        try:
            await self._redis.set(
                cache_key(query, top_k, scope=scope),
                json.dumps([c.model_dump(mode="json") for c in chunks], ensure_ascii=False),
                ex=self._ttl,
            )
        except Exception:
            logger.warning("retrieval_cache_redis_set_failed")
