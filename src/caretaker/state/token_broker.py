"""Redis-backed installation token broker.

Wraps :class:`~caretaker.github_app.installation_tokens.InstallationTokenMinter`
with a Redis caching layer so that multiple replicas share token state and
stay within GitHub's API rate limits.

When Redis is not configured the broker delegates directly to the in-process
``InstallationTokenCache`` that is already present in
:mod:`~caretaker.github_app.installation_tokens`, preserving single-replica
behaviour unchanged.

SaaS free-tier options for Redis:
- Upstash (https://upstash.com) — 10 K commands/day
- Redis Cloud (https://redis.io/cloud) — 30 MB free
Both accept a standard ``REDIS_URL`` / ``rediss://...`` URL.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import TYPE_CHECKING

from caretaker.github_app.installation_tokens import (
    InstallationToken,
    InstallationTokenCache,
    InstallationTokenMinter,
)
from caretaker.github_app.jwt_signer import AppJWTSigner

if TYPE_CHECKING:
    import redis.asyncio

logger = logging.getLogger(__name__)

_KEY_PREFIX = "caretaker:token:install:"
_DEFAULT_SKEW_SECONDS = 5 * 60  # refresh 5 min before expiry
_DEFAULT_CACHE_TTL_SECONDS = 3000  # < 3600 s token lifetime


class RedisTokenCache(InstallationTokenCache):
    """``InstallationTokenCache`` subclass that stores tokens in Redis.

    Falls back to the parent in-process dict when a Redis call fails so
    we never crash the process just because Redis is momentarily down.
    """

    def __init__(
        self,
        redis_url: str,
        ttl_seconds: int = _DEFAULT_CACHE_TTL_SECONDS,
        key_prefix: str = _KEY_PREFIX,
    ) -> None:
        super().__init__()
        self._redis_url = redis_url
        self._ttl_seconds = ttl_seconds
        self._key_prefix = key_prefix
        self._redis: redis.asyncio.Redis | None = None  # type: ignore[type-arg]
        self._init_lock = asyncio.Lock()

    async def _client(self) -> redis.asyncio.Redis:  # type: ignore[type-arg]
        if self._redis is None:
            async with self._init_lock:
                if self._redis is None:
                    import redis.asyncio as aioredis

                    self._redis = aioredis.from_url(
                        self._redis_url,
                        decode_responses=True,
                        socket_connect_timeout=3,
                        socket_timeout=3,
                    )
        return self._redis

    def _key(self, installation_id: int) -> str:
        return f"{self._key_prefix}{installation_id}"

    async def get(self, installation_id: int) -> InstallationToken | None:
        try:
            client = await self._client()
            raw = await client.get(self._key(installation_id))
            if raw:
                data = json.loads(raw)
                return InstallationToken(
                    token=data["token"],
                    expires_at=data["expires_at"],
                    installation_id=installation_id,
                )
        except Exception:
            logger.warning(
                "RedisTokenCache.get failed for installation %d, using in-process cache",
                installation_id,
                exc_info=True,
            )
        # fall through to in-process cache
        return await super().get(installation_id)

    async def put(self, token: InstallationToken) -> None:
        try:
            client = await self._client()
            raw = json.dumps({"token": token.token, "expires_at": token.expires_at})
            ttl = max(
                1,
                min(self._ttl_seconds, token.expires_at - int(time.time()) - 60),
            )
            await client.set(self._key(token.installation_id), raw, ex=ttl)
        except Exception:
            logger.warning(
                "RedisTokenCache.put failed for installation %d, storing in-process only",
                token.installation_id,
                exc_info=True,
            )
        # always keep in-process copy too
        await super().put(token)

    async def invalidate(self, installation_id: int) -> None:
        try:
            client = await self._client()
            await client.delete(self._key(installation_id))
        except Exception:
            logger.warning(
                "RedisTokenCache.invalidate failed for installation %d",
                installation_id,
                exc_info=True,
            )
        await super().invalidate(installation_id)

    async def close(self) -> None:
        if self._redis is not None:
            await self._redis.aclose()
            self._redis = None


def build_token_broker(
    *,
    app_id_env: str = "CARETAKER_GITHUB_APP_ID",
    private_key_env: str = "CARETAKER_GITHUB_APP_PRIVATE_KEY",
    private_key_path_env: str = "CARETAKER_GITHUB_APP_PRIVATE_KEY_PATH",
    redis_url_env: str = "REDIS_URL",
    cache_ttl_seconds: int = _DEFAULT_CACHE_TTL_SECONDS,
    refresh_skew_seconds: int = _DEFAULT_SKEW_SECONDS,
) -> InstallationTokenMinter | None:
    """Build an ``InstallationTokenMinter`` with optional Redis cache.

    Returns ``None`` when the GitHub App is not configured (i.e. neither
    ``CARETAKER_GITHUB_APP_ID`` nor a private key is set in the environment).
    """
    app_id_str = os.environ.get(app_id_env, "").strip()
    if not app_id_str:
        logger.debug("Token broker: %s not set, GitHub App not configured", app_id_env)
        return None

    try:
        app_id = int(app_id_str)
    except ValueError:
        logger.error("Token broker: %s=%r is not a valid integer", app_id_env, app_id_str)
        return None

    # Resolve private key — prefer inline PEM over file path
    private_key_pem = os.environ.get(private_key_env, "").strip()
    if not private_key_pem:
        key_path = os.environ.get(private_key_path_env, "").strip()
        if key_path:
            try:
                with open(key_path) as fh:
                    private_key_pem = fh.read().strip()
            except OSError as exc:
                logger.error("Token broker: cannot read private key from %r: %s", key_path, exc)
                return None
    if not private_key_pem:
        logger.debug("Token broker: no private key configured")
        return None

    signer = AppJWTSigner(app_id=app_id, private_key_pem=private_key_pem)

    redis_url = os.environ.get(redis_url_env, "").strip()
    if redis_url:
        logger.info("Token broker: using Redis-backed cache (%s)", redis_url_env)
        cache: InstallationTokenCache = RedisTokenCache(
            redis_url=redis_url,
            ttl_seconds=cache_ttl_seconds,
        )
    else:
        logger.info("Token broker: Redis not configured, using in-process cache")
        cache = InstallationTokenCache()

    return InstallationTokenMinter(
        signer=signer,
        cache=cache,
        refresh_skew_seconds=refresh_skew_seconds,
    )
