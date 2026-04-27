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
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MemoryEntry:
    """Single row returned by :meth:`MemoryStore.query`."""

    namespace: str
    key: str
    value: str
    created_at: datetime
    updated_at: datetime
    expires_at: datetime | None


def _parse_dt(value: str) -> datetime:
    """Parse an ISO-formatted timestamp, forcing UTC when naive."""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _row_to_entry(row: tuple[Any, ...]) -> MemoryEntry:
    namespace, key, value, created_at, updated_at, expires_at = row
    return MemoryEntry(
        namespace=namespace,
        key=key,
        value=value,
        created_at=_parse_dt(created_at),
        updated_at=_parse_dt(updated_at),
        expires_at=_parse_dt(expires_at) if expires_at is not None else None,
    )


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
CREATE INDEX IF NOT EXISTS idx_memory_ns_updated ON memory(namespace, updated_at);
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

    def query(
        self,
        namespace_glob: str,
        since: datetime | None = None,
        limit: int = 100,
    ) -> list[MemoryEntry]:
        """Return entries whose namespace matches ``namespace_glob``.

        Uses SQLite's ``GLOB`` operator for shell-style pattern matching
        (e.g. ``pr-*`` matches every PR-agent namespace). Expired rows
        are filtered out. The ``since`` cutoff compares against
        ``updated_at`` so callers can tail the store without paging all
        history. Ordered newest-first (``updated_at DESC``).

        Args:
            namespace_glob: SQLite GLOB pattern. ``"*"`` returns every
                namespace; ``"pr-*"`` only the PR-prefixed ones.
            since: Optional UTC cutoff — only rows with
                ``updated_at > since`` are returned.
            limit: Maximum number of rows to return (``> 0``).
        """
        if limit <= 0:
            return []
        now_iso = datetime.now(UTC).isoformat()
        params: list[Any] = [namespace_glob, now_iso]
        clauses = [
            "namespace GLOB ?",
            "(expires_at IS NULL OR expires_at > ?)",
        ]
        if since is not None:
            if since.tzinfo is None:
                since = since.replace(tzinfo=UTC)
            clauses.append("updated_at > ?")
            params.append(since.isoformat())
        where = " AND ".join(clauses)
        query = (
            "SELECT namespace, key, value, created_at, updated_at, expires_at "
            "FROM memory "
            f"WHERE {where} "
            "ORDER BY updated_at DESC "
            "LIMIT ?"
        )
        params.append(limit)
        rows = self._conn.execute(query, params).fetchall()
        return [_row_to_entry(row) for row in rows]

    def recent_keys(self, namespace: str, n: int = 10) -> list[str]:
        """Return the last ``n`` keys in ``namespace`` by ``updated_at`` desc.

        A thin helper over :meth:`list_keys` that caps the result size so
        agents can tail their own ring buffer without paging the whole
        namespace. Expired rows are filtered out.
        """
        if n <= 0:
            return []
        now = datetime.now(UTC).isoformat()
        rows = self._conn.execute(
            """
            SELECT key FROM memory
            WHERE namespace=? AND (expires_at IS NULL OR expires_at > ?)
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (namespace, now, n),
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
