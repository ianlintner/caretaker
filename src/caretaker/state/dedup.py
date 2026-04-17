"""Redis-backed webhook delivery deduplication.

Provides idempotent webhook handling across multiple replicas using atomic
``SET NX PX`` operations.  Falls back gracefully to process-local dedup
when Redis is not configured, preserving single-replica behaviour.

SaaS free-tier options that work with a standard ``REDIS_URL``:
- Upstash (https://upstash.com)  — 10 K commands/day, 256 MB
- Redis Cloud (https://redis.io/cloud) — 30 MB free database
"""

from __future__ import annotations

import asyncio
import collections
import logging
import os
from typing import TYPE_CHECKING

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    pass


class RedisDedup:
    """Webhook delivery dedup backed by Redis ``SET NX PX``.

    Each delivery id is stored as a Redis key with a TTL so memory stays
    bounded.  Two instances pointing at the same Redis will agree on
    which deliveries are new—enabling horizontal scaling.

    Parameters
    ----------
    redis_url:
        Fully-qualified Redis URL, e.g. ``rediss://...`` (TLS) or
        ``redis://localhost:6379``.
    ttl_seconds:
        How long to remember a delivery id.  Deliveries arriving after
        the TTL would be treated as new (very unlikely given GitHub's
        retry window of 72 h, so keep at ≥ 3600).
    key_prefix:
        Namespace prefix; use different values when sharing one Redis
        with multiple services.
    """

    def __init__(
        self,
        redis_url: str,
        ttl_seconds: int = 3600,
        key_prefix: str = "caretaker:dedup:",
    ) -> None:
        self._redis_url = redis_url
        self._ttl_seconds = ttl_seconds
        self._key_prefix = key_prefix
        self._client: "redis.asyncio.Redis | None" = None  # type: ignore[type-arg]
        self._lock = asyncio.Lock()

    async def _get_client(self) -> "redis.asyncio.Redis":  # type: ignore[type-arg]
        """Return a lazily-initialised Redis client."""
        if self._client is None:
            async with self._lock:
                if self._client is None:
                    import redis.asyncio as aioredis

                    self._client = aioredis.from_url(
                        self._redis_url,
                        decode_responses=True,
                        socket_connect_timeout=5,
                        socket_timeout=5,
                    )
        return self._client

    async def is_new(self, delivery_id: str) -> bool:
        """Return ``True`` if this delivery id has not been seen before.

        Uses ``SET key 1 NX PX <ms>`` for an atomic check-and-set.
        """
        client = await self._get_client()
        key = f"{self._key_prefix}{delivery_id}"
        ttl_ms = self._ttl_seconds * 1000
        # SET NX returns True when the key was newly set (delivery is new)
        result = await client.set(key, "1", nx=True, px=ttl_ms)
        return bool(result)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


class LocalDedup:
    """Process-local LRU-bounded dedup — zero dependencies.

    Used as the fallback when Redis is not configured.
    """

    def __init__(self, capacity: int = 2048) -> None:
        self._capacity = capacity
        self._seen: collections.deque[str] = collections.deque()
        self._seen_set: set[str] = set()
        self._lock = asyncio.Lock()

    async def is_new(self, delivery_id: str) -> bool:
        async with self._lock:
            if delivery_id in self._seen_set:
                return False
            if len(self._seen) >= self._capacity:
                evicted = self._seen.popleft()
                self._seen_set.discard(evicted)
            self._seen.append(delivery_id)
            self._seen_set.add(delivery_id)
            return True

    async def close(self) -> None:
        pass


def build_dedup(
    redis_url_env: str = "REDIS_URL",
    ttl_seconds: int = 3600,
    key_prefix: str = "caretaker:dedup:",
    fallback_capacity: int = 2048,
) -> "RedisDedup | LocalDedup":
    """Build a dedup backend from the environment.

    Returns a ``RedisDedup`` when ``redis_url_env`` is set in the environment,
    otherwise returns a ``LocalDedup``.
    """
    redis_url = os.environ.get(redis_url_env, "").strip()
    if redis_url:
        logger.info("Webhook dedup: using Redis (%s)", redis_url_env)
        return RedisDedup(redis_url=redis_url, ttl_seconds=ttl_seconds, key_prefix=key_prefix)
    logger.info("Webhook dedup: Redis not configured — using in-process dedup")
    return LocalDedup(capacity=fallback_capacity)
