"""Redis MemoryBackend — Upstash / Redis Cloud / local Redis.

Enabled when ``memory_store.backend = "redis"`` in ``.caretaker.yml``.
Requires the ``redis`` package (already a core dependency).

Connection URL is read from the env var named in
``redis.redis_url_env`` (default: ``REDIS_URL``). This works with:

- **Upstash** (https://upstash.com) — free tier: 10 K commands/day.
- **Redis Cloud** (https://redis.com) — free tier.
- Any standard ``redis-server`` instance: ``redis://localhost:6379/``.

Redis natively handles expiration via ``SET ... EX``, so ``prune_expired()``
is a no-op (returns 0). Keys are stored as ``caretaker:memory:{ns}:{key}``
with the colon convention so operators can inspect them with ``KEYS`` or
``SCAN`` in redis-cli.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import redis


logger = logging.getLogger(__name__)

_KEY_PREFIX = "caretaker:memory"


def _redis_key(namespace: str, key: str) -> str:
    """Build a colon-delimited Redis key."""
    return f"{_KEY_PREFIX}:{namespace}:{key}"


def _namespace_pattern(namespace: str) -> str:
    """SCAN pattern for all keys in a namespace."""
    return f"{_KEY_PREFIX}:{namespace}:*"


class RedisMemoryBackend:
    """Redis-backed namespaced key-value store.

    Uses the synchronous ``redis`` client so the interface matches
    ``MemoryBackend`` (no async leakage into agent code).

    Args:
        redis_url: Standard Redis connection URL.
        max_entries_per_namespace: Prune oldest entries when this limit is
            exceeded per namespace.  ``0`` disables the limit.
    """

    def __init__(
        self,
        redis_url: str,
        max_entries_per_namespace: int = 1000,
    ) -> None:
        try:
            import redis as _redis
        except ImportError as exc:  # pragma: no cover
            raise ImportError(
                "redis is required for the Redis memory backend. "
                "Install it with: pip install caretaker-github[backend]"
            ) from exc

        self._max_entries = max_entries_per_namespace
        self._client: redis.Redis[str] = _redis.Redis.from_url(redis_url, decode_responses=True)
        # Verify connectivity eagerly so misconfiguration surfaces at startup.
        try:
            self._client.ping()
        except Exception:
            logger.warning("RedisMemoryBackend ping failed; backend may not be reachable")
        else:
            logger.info("RedisMemoryBackend connected (%s)", _redact_url(redis_url))

    # ── Read ──────────────────────────────────────────────────────────────

    def get(self, namespace: str, key: str) -> str | None:
        return self._client.get(_redis_key(namespace, key))

    def get_json(self, namespace: str, key: str) -> Any:
        raw = self.get(namespace, key)
        if raw is None:
            return None
        return json.loads(raw)

    def list_keys(self, namespace: str) -> list[str]:
        """Return all keys in *namespace*, ordered newest-first.

        Uses SCAN with a namespace pattern, then fetches the object
        idle time for each key to approximate last-write ordering.
        Falls back to alphabetical when OBJECT IDLETIME is unavailable.
        """
        prefix = _KEY_PREFIX + ":" + namespace + ":"
        prefix_len = len(prefix)
        keys: list[str] = []
        cursor = 0
        while True:
            cursor, batch = self._client.scan(
                cursor,
                match=_namespace_pattern(namespace),
                count=100,
            )
            keys.extend(k[prefix_len:] for k in batch)
            if cursor == 0:
                break

        if not keys:
            return []

        # Order by most-recently-written first, using Redis OBJECT IDLETIME.
        # Lower idle time → written more recently.
        try:
            pipe = self._client.pipeline(transaction=False)
            for k in keys:
                pipe.object("IDLETIME", _redis_key(namespace, k))
            idle_times = pipe.execute()
            paired = list(zip(keys, idle_times, strict=False))
            # Sort: None first (key doesn't exist / no IDLETIME support),
            # then by idle time ascending (more recent = smaller idle).
            paired.sort(key=lambda x: (x[1] is not None, x[1] if x[1] is not None else 0))
            return [p[0] for p in paired]
        except Exception:
            # OBJECT IDLETIME not supported (e.g. some managed Redis).
            return sorted(keys, reverse=True)

    def all_entries(self, namespace: str) -> dict[str, str]:
        keys = self.list_keys(namespace)
        if not keys:
            return {}
        redis_keys = [_redis_key(namespace, k) for k in keys]
        values = self._client.mget(redis_keys)
        return {k: str(v) for k, v in zip(keys, values, strict=False) if v is not None}

    # ── Write ─────────────────────────────────────────────────────────────

    def set(
        self,
        namespace: str,
        key: str,
        value: str,
        ttl_seconds: int | None = None,
    ) -> None:
        rk = _redis_key(namespace, key)
        if ttl_seconds is not None:
            self._client.set(rk, value, ex=ttl_seconds)
        else:
            self._client.set(rk, value)
        if self._max_entries > 0:
            self._enforce_namespace_limit(namespace)

    def set_json(
        self,
        namespace: str,
        key: str,
        value: Any,
        ttl_seconds: int | None = None,
    ) -> None:
        self.set(namespace, key, json.dumps(value), ttl_seconds=ttl_seconds)

    def delete(self, namespace: str, key: str) -> None:
        self._client.delete(_redis_key(namespace, key))

    # ── Maintenance ───────────────────────────────────────────────────────

    def _enforce_namespace_limit(self, namespace: str) -> None:
        """Prune oldest entries when the per-namespace cap is exceeded.

        Uses Redis OBJECT IDLETIME to find the least-recently-written keys
        and deletes the excess. Falls back to alphabetical if IDLETIME
        is unavailable (rare — most managed Redis providers support it).
        """
        keys = self.list_keys(namespace)
        excess = len(keys) - self._max_entries
        if excess <= 0:
            return
        oldest = keys[-excess:]  # list_keys returns newest-first
        if oldest:
            self._client.delete(*[_redis_key(namespace, k) for k in oldest])

    def prune_expired(self) -> int:
        """No-op: Redis handles expiry natively via ``SET ... EX``.

        Returns 0 (Protocol compliance). Kept so code that calls
        ``backend.prune_expired()`` does not break when the backend is swapped.
        """
        return 0

    def snapshot_json(self) -> str:
        """Return all entries as a JSON string (for workflow artifacts).

        Uses SCAN to iterate all ``caretaker:memory:*`` keys. For large
        stores this may be slow — snapshot is a best-effort debug tool,
        not a production data path.
        """
        data: dict[str, list[dict[str, str | None]]] = {}
        cursor = 0
        while True:
            cursor, batch = self._client.scan(
                cursor,
                match=f"{_KEY_PREFIX}:*",
                count=100,
            )
            if batch:
                pipe = self._client.pipeline(transaction=False)
                for key_name in batch:
                    pipe.get(key_name)
                    pipe.ttl(key_name)
                results = pipe.execute()
                # results interleaved: [val0, ttl0, val1, ttl1, ...]
                for i in range(0, len(results), 2):
                    val = results[i]
                    ttl_val = results[i + 1]
                    if val is None:
                        continue
                    rk: str = batch[i // 2]
                    # Parse namespace and key from the redis key.
                    # Format: caretaker:memory:<ns>:<key>
                    parts = rk.split(":", 2)  # ["caretaker", "memory", "<ns>:<key>"]
                    if len(parts) < 3:
                        continue
                    ns_and_key = parts[2].split(":", 1)
                    if len(ns_and_key) < 2:
                        continue
                    ns, entry_key = ns_and_key[0], ns_and_key[1]
                    data.setdefault(ns, []).append(
                        {
                            "key": entry_key,
                            "value": str(val),
                            "created_at": None,
                            "updated_at": None,
                            "expires_at": (
                                (datetime.now(UTC) + timedelta(seconds=ttl_val)).isoformat()
                                if ttl_val > 0
                                else None
                            ),
                        }
                    )
            if cursor == 0:
                break
        # Sort each namespace's entries by key for stable output.
        for ns_entries in data.values():
            ns_entries.sort(key=lambda e: e["key"] or "")
        return json.dumps(data, indent=2)

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._client.close()


def _redact_url(url: str) -> str:
    """Return a safe-for-logs version of the Redis URL (password redacted)."""
    if "@" in url:
        prefix, rest = url.split("@", 1)
        if "://" in prefix:
            scheme, creds = prefix.split("://", 1)
            if ":" in creds:
                user, _ = creds.split(":", 1)
                return f"{scheme}://{user}:****@{rest}"
            return f"{scheme}://{creds}@{rest}"
    return url


def build_redis_backend(
    redis_url_env: str = "REDIS_URL",
    max_entries_per_namespace: int = 1000,
) -> RedisMemoryBackend:
    """Construct a ``RedisMemoryBackend`` from environment variables.

    Raises ``RuntimeError`` if the required env var is unset.
    """
    redis_url = os.environ.get(redis_url_env, "").strip()
    if not redis_url:
        raise RuntimeError(
            f"Environment variable '{redis_url_env}' is not set. "
            "Set it to a Redis connection URL, e.g.:\n"
            "  rediss://default:pass@host:port   (Upstash / Redis Cloud)\n"
            "  redis://localhost:6379/           (local)"
        )
    return RedisMemoryBackend(
        redis_url=redis_url,
        max_entries_per_namespace=max_entries_per_namespace,
    )
