"""PostgreSQL MemoryBackend — SaaS Postgres (Neon / Supabase / any standard PG).

Enabled when ``memory_store.backend = "postgres"`` in ``.caretaker.yml``.
Requires the ``psycopg[binary]`` package (installed via the ``backend``
extra: ``pip install caretaker[backend]``).

Connection URL is read from the env var named in
``postgres.database_url_env`` (default: ``DATABASE_URL``).  This works
with:

- **Neon** (https://neon.tech) — free 0.5 GB tier, serverless.
- **Supabase** (https://supabase.com) — free tier.
- **Railway** — Postgres add-on.
- Any self-hosted Postgres that accepts a standard libpq URL.

The URL format must be compatible with psycopg3 async::

    postgresql+psycopg://user:pass@host/dbname?sslmode=require

(``sslmode=require`` is strongly recommended for SaaS providers.)

Schema is managed via Alembic migrations in ``alembic/``.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    import psycopg


class PostgresMemoryBackend:
    """Postgres-backed namespaced key-value store.

    Uses a synchronous ``psycopg`` connection under the hood so the interface
    matches ``SQLiteMemoryBackend`` (no async leakage into agent code).

    Args:
        database_url: libpq-compatible connection URL.
        max_entries_per_namespace: Prune oldest entries when this limit is
            exceeded per namespace.  ``0`` disables the limit.
    """

    def __init__(self, database_url: str, max_entries_per_namespace: int = 1000) -> None:
        try:
            import psycopg as _psycopg
        except ImportError as exc:  # pragma: no cover
            raise ImportError(
                "psycopg is required for the postgres memory backend. "
                "Install it with: pip install caretaker[backend]"
            ) from exc

        self._max_entries = max_entries_per_namespace
        # psycopg3 accepts postgresql:// and postgresql+psycopg://
        url = database_url.replace("postgresql+psycopg://", "postgresql://")
        self._conn: psycopg.Connection[Any] = _psycopg.connect(url, autocommit=False)
        self._ensure_schema()
        logger.info("PostgresMemoryBackend connected")

    # ── Schema bootstrap ─────────────────────────────────────────────────

    def _ensure_schema(self) -> None:
        """Create the memory table if it does not already exist.

        Alembic migrations handle schema evolution after initial creation.
        This guard keeps the backend self-bootstrapping for CI / tests.
        """
        with self._conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS caretaker_memory (
                    namespace   TEXT    NOT NULL,
                    key         TEXT    NOT NULL,
                    value       TEXT    NOT NULL,
                    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
                    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
                    expires_at  TIMESTAMPTZ,
                    PRIMARY KEY (namespace, key)
                )
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_caretaker_memory_ns_updated
                ON caretaker_memory (namespace, updated_at DESC)
                """
            )
        self._conn.commit()

    # ── Read ──────────────────────────────────────────────────────────────

    def get(self, namespace: str, key: str) -> str | None:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT value, expires_at FROM caretaker_memory "
                "WHERE namespace=%s AND key=%s",
                (namespace, key),
            )
            row = cur.fetchone()
        if row is None:
            return None
        value, expires_at = row
        if expires_at is not None:
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=UTC)
            if expires_at <= datetime.now(UTC):
                self.delete(namespace, key)
                return None
        return str(value)

    def get_json(self, namespace: str, key: str) -> Any:
        raw = self.get(namespace, key)
        if raw is None:
            return None
        return json.loads(raw)

    def list_keys(self, namespace: str) -> list[str]:
        now = datetime.now(UTC)
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT key FROM caretaker_memory
                WHERE namespace=%s AND (expires_at IS NULL OR expires_at > %s)
                ORDER BY updated_at DESC
                """,
                (namespace, now),
            )
            return [row[0] for row in cur.fetchall()]

    def all_entries(self, namespace: str) -> dict[str, str]:
        now = datetime.now(UTC)
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT key, value FROM caretaker_memory
                WHERE namespace=%s AND (expires_at IS NULL OR expires_at > %s)
                """,
                (namespace, now),
            )
            return {row[0]: str(row[1]) for row in cur.fetchall()}

    # ── Write ─────────────────────────────────────────────────────────────

    def set(
        self,
        namespace: str,
        key: str,
        value: str,
        ttl_seconds: int | None = None,
    ) -> None:
        now = datetime.now(UTC)
        expires_at: datetime | None = None
        if ttl_seconds is not None:
            expires_at = now + timedelta(seconds=ttl_seconds)

        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO caretaker_memory
                    (namespace, key, value, created_at, updated_at, expires_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (namespace, key) DO UPDATE SET
                    value      = EXCLUDED.value,
                    updated_at = EXCLUDED.updated_at,
                    expires_at = EXCLUDED.expires_at
                """,
                (namespace, key, value, now, now, expires_at),
            )
        self._conn.commit()
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
        with self._conn.cursor() as cur:
            cur.execute(
                "DELETE FROM caretaker_memory WHERE namespace=%s AND key=%s",
                (namespace, key),
            )
        self._conn.commit()

    # ── Maintenance ───────────────────────────────────────────────────────

    def _enforce_namespace_limit(self, namespace: str) -> None:
        """Prune the oldest entries when the per-namespace cap is exceeded."""
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM caretaker_memory WHERE namespace=%s",
                (namespace,),
            )
            row = cur.fetchone()
            count = row[0] if row else 0
            if count <= self._max_entries:
                return
            excess = count - self._max_entries
            cur.execute(
                """
                DELETE FROM caretaker_memory
                WHERE (namespace, key) IN (
                    SELECT namespace, key FROM caretaker_memory
                    WHERE namespace=%s
                    ORDER BY updated_at ASC
                    LIMIT %s
                )
                """,
                (namespace, excess),
            )
        self._conn.commit()

    def prune_expired(self) -> int:
        with self._conn.cursor() as cur:
            cur.execute(
                "DELETE FROM caretaker_memory "
                "WHERE expires_at IS NOT NULL AND expires_at <= %s",
                (datetime.now(UTC),),
            )
            removed = cur.rowcount
        self._conn.commit()
        if removed:
            logger.debug("PostgresMemoryBackend pruned %d expired entries", removed)
        return removed

    def snapshot_json(self) -> str:
        """Return all non-expired entries as a JSON string (for workflow artifacts)."""
        now = datetime.now(UTC)
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT namespace, key, value, created_at, updated_at, expires_at
                FROM caretaker_memory
                WHERE expires_at IS NULL OR expires_at > %s
                ORDER BY namespace, updated_at DESC
                """,
                (now,),
            )
            rows = cur.fetchall()

        data: dict[str, list[dict[str, str | None]]] = {}
        for ns, key, value, created_at, updated_at, expires_at in rows:
            data.setdefault(ns, []).append(
                {
                    "key": key,
                    "value": value,
                    "created_at": created_at.isoformat() if created_at else None,
                    "updated_at": updated_at.isoformat() if updated_at else None,
                    "expires_at": expires_at.isoformat() if expires_at else None,
                }
            )
        return json.dumps(data, indent=2)

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass


def build_postgres_backend(
    database_url_env: str = "DATABASE_URL",
    max_entries_per_namespace: int = 1000,
) -> PostgresMemoryBackend:
    """Construct a ``PostgresMemoryBackend`` from environment variables.

    Raises ``RuntimeError`` if the required env var is unset.
    """
    url = os.environ.get(database_url_env, "").strip()
    if not url:
        raise RuntimeError(
            f"Postgres memory backend requires env var '{database_url_env}' to be set. "
            "Sign up for a free Neon account at https://neon.tech and copy your "
            "connection string into the env var."
        )
    return PostgresMemoryBackend(url, max_entries_per_namespace=max_entries_per_namespace)
