"""Disk-backed memory store for caretaker agents.

Provides a lightweight SQLite key-value store that persists between workflow
runs when the database file is cached (e.g. via ``actions/cache``).  Each
entry belongs to a *namespace* so individual agents can write without
colliding with one another.

Usage::

    store = MemoryStore("/tmp/caretaker-memory.db")
    store.set("my-agent", "last_run", "2024-01-01T00:00:00Z")
    value = store.get("my-agent", "last_run")
    store.close()
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DDL = """
CREATE TABLE IF NOT EXISTS memory (
    namespace   TEXT    NOT NULL,
    key         TEXT    NOT NULL,
    value       TEXT    NOT NULL,
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL,
    expires_at  TEXT,
    PRIMARY KEY (namespace, key)
);
"""


class MemoryStore:
    """SQLite-backed namespaced key-value store for agent memory.

    Instances are not thread-safe — create one per thread/coroutine context or
    protect access with a lock.  Within a single workflow job this is always
    fine because caretaker runs its agents sequentially.

    Args:
        db_path: Path to the SQLite database file.  Created automatically if
            it does not exist.  Pass ``":memory:"`` for an ephemeral in-process
            store (useful in tests).
        max_entries_per_namespace: When saving a value would exceed this limit,
            the oldest entries (by ``updated_at``) are pruned first.  Set to 0
            to disable the limit.
    """

    def __init__(self, db_path: str = ":memory:", max_entries_per_namespace: int = 1000) -> None:
        self._db_path = db_path
        self._max_entries = max_entries_per_namespace
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_DDL)
        self._conn.commit()
        logger.debug("MemoryStore opened: %s", db_path)

    # ── Public API ────────────────────────────────────────────────────────

    def get(self, namespace: str, key: str) -> str | None:
        """Return the stored value or ``None`` if absent / expired."""
        row = self._conn.execute(
            "SELECT value, expires_at FROM memory WHERE namespace=? AND key=?",
            (namespace, key),
        ).fetchone()
        if row is None:
            return None
        value, expires_at = row
        if expires_at is not None and datetime.fromisoformat(expires_at) <= datetime.now(UTC):
            self.delete(namespace, key)
            return None
        return str(value)

    def get_json(self, namespace: str, key: str) -> Any:
        """Return the stored value parsed from JSON, or ``None`` if absent."""
        raw = self.get(namespace, key)
        if raw is None:
            return None
        return json.loads(raw)

    def set(
        self,
        namespace: str,
        key: str,
        value: str,
        ttl_seconds: int | None = None,
    ) -> None:
        """Store *value* under *namespace*/*key*.

        Args:
            namespace: Agent name or logical grouping.
            key: Entry key within the namespace.
            value: String value to store.
            ttl_seconds: Optional time-to-live in seconds.  Expired entries
                are treated as absent and lazily deleted on ``get``.
        """
        now = datetime.now(UTC).isoformat()
        expires_at: str | None = None
        if ttl_seconds is not None:
            expires_at = (datetime.now(UTC) + timedelta(seconds=ttl_seconds)).isoformat()

        self._conn.execute(
            """
            INSERT INTO memory (namespace, key, value, created_at, updated_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(namespace, key) DO UPDATE SET
                value      = excluded.value,
                updated_at = excluded.updated_at,
                expires_at = excluded.expires_at
            """,
            (namespace, key, value, now, now, expires_at),
        )
        self._conn.commit()
        self._enforce_namespace_limit(namespace)

    def set_json(
        self,
        namespace: str,
        key: str,
        value: Any,
        ttl_seconds: int | None = None,
    ) -> None:
        """Serialize *value* as JSON then store it."""
        self.set(namespace, key, json.dumps(value), ttl_seconds=ttl_seconds)

    def delete(self, namespace: str, key: str) -> None:
        """Remove a single entry.  No-op if it does not exist."""
        self._conn.execute("DELETE FROM memory WHERE namespace=? AND key=?", (namespace, key))
        self._conn.commit()

    def list_keys(self, namespace: str) -> list[str]:
        """Return all non-expired keys in *namespace*, ordered by ``updated_at`` desc."""
        now = datetime.now(UTC).isoformat()
        rows = self._conn.execute(
            """
            SELECT key FROM memory
            WHERE namespace=? AND (expires_at IS NULL OR expires_at > ?)
            ORDER BY updated_at DESC
            """,
            (namespace, now),
        ).fetchall()
        return [row[0] for row in rows]

    def all_entries(self, namespace: str) -> dict[str, str]:
        """Return all non-expired key→value pairs in *namespace*."""
        now = datetime.now(UTC).isoformat()
        rows = self._conn.execute(
            """
            SELECT key, value FROM memory
            WHERE namespace=? AND (expires_at IS NULL OR expires_at > ?)
            """,
            (namespace, now),
        ).fetchall()
        return {row[0]: row[1] for row in rows}

    def prune_expired(self) -> int:
        """Delete all expired entries and return the count removed."""
        now = datetime.now(UTC).isoformat()
        cursor = self._conn.execute(
            "DELETE FROM memory WHERE expires_at IS NOT NULL AND expires_at <= ?", (now,)
        )
        self._conn.commit()
        removed = cursor.rowcount
        if removed:
            logger.debug("MemoryStore pruned %d expired entries", removed)
        return removed

    def snapshot_json(self) -> str:
        """Return the full (non-expired) store contents as a JSON string."""
        now = datetime.now(UTC).isoformat()
        rows = self._conn.execute(
            """
            SELECT namespace, key, value, created_at, updated_at, expires_at
            FROM memory
            WHERE expires_at IS NULL OR expires_at > ?
            ORDER BY namespace, updated_at DESC
            """,
            (now,),
        ).fetchall()
        data: dict[str, list[dict[str, str | None]]] = {}
        for ns, key, value, created_at, updated_at, expires_at in rows:
            data.setdefault(ns, []).append(
                {
                    "key": key,
                    "value": value,
                    "created_at": created_at,
                    "updated_at": updated_at,
                    "expires_at": expires_at,
                }
            )
        return json.dumps(data, indent=2)

    def close(self) -> None:
        """Close the underlying SQLite connection."""
        self._conn.close()
        logger.debug("MemoryStore closed: %s", self._db_path)

    # ── Context manager ───────────────────────────────────────────────────

    def __enter__(self) -> MemoryStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # ── Private helpers ───────────────────────────────────────────────────

    def _enforce_namespace_limit(self, namespace: str) -> None:
        """Prune oldest entries in *namespace* if the per-namespace cap is exceeded."""
        if self._max_entries <= 0:
            return
        count = self._conn.execute(
            "SELECT COUNT(*) FROM memory WHERE namespace=?", (namespace,)
        ).fetchone()[0]
        if count <= self._max_entries:
            return
        excess = count - self._max_entries
        self._conn.execute(
            """
            DELETE FROM memory WHERE (namespace, key) IN (
                SELECT namespace, key FROM memory
                WHERE namespace=?
                ORDER BY updated_at ASC
                LIMIT ?
            )
            """,
            (namespace, excess),
        )
        self._conn.commit()
        logger.debug("MemoryStore pruned %d old entries from namespace '%s'", excess, namespace)
