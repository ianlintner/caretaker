"""SQLite-backed fleet registry store.

Same async interface as :class:`caretaker.fleet.store.FleetRegistryStore`
but durable across process restarts. The receiver is a single-replica
deployment so a local file-backed SQLite database is sufficient; we
still serialise writes through an :class:`asyncio.Lock` to keep the
in-process semantics identical to the in-memory variant.

Schema
------

Two tables, both keyed by repo slug:

``fleet_clients``
    One row per known consumer repo. Mirrors the :class:`FleetClient`
    dataclass; ``last_summary``/``last_counters``/``enabled_agents``
    are stored as JSON text columns.

``fleet_heartbeats``
    Append-only history table. After every insert we prune anything
    older than the most-recent ``_HEARTBEAT_HISTORY_MAXLEN`` rows for
    that repo, mirroring the in-memory ``deque(maxlen=32)`` semantics.

Both tables are created on first connect. Migrations are handled by
``CREATE TABLE IF NOT EXISTS`` plus best-effort ``ALTER TABLE`` for
forward compatibility.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .store import _HEARTBEAT_HISTORY_MAXLEN, FleetClient

if TYPE_CHECKING:
    from collections.abc import Iterable

logger = logging.getLogger(__name__)


DEFAULT_DB_PATH_ENV = "CARETAKER_FLEET_DB_PATH"
DEFAULT_DB_RELATIVE = ".local/state/caretaker/fleet-registry.db"


def resolve_db_path(env_value: str | None = None) -> Path:
    """Compute the SQLite database path.

    Honours ``CARETAKER_FLEET_DB_PATH``, falling back to
    ``~/.local/state/caretaker/fleet-registry.db``. The special string
    ``":memory:"`` is preserved verbatim so callers can request an
    in-memory database.
    """
    raw = env_value if env_value is not None else os.environ.get(DEFAULT_DB_PATH_ENV)
    if raw and raw.strip():
        candidate = raw.strip()
        if candidate == ":memory:":
            return Path(candidate)
        return Path(candidate).expanduser()
    return Path.home() / DEFAULT_DB_RELATIVE


class SQLiteFleetRegistryStore:
    """Durable, async-safe fleet registry backed by SQLite.

    All public methods are coroutines and offer the same signatures as
    the in-memory :class:`FleetRegistryStore`. Internally the synchronous
    ``sqlite3`` module is used inside ``asyncio.to_thread`` calls; this is
    simpler than introducing an ``aiosqlite`` dependency and the heartbeat
    receiver never sees enough write volume to require a true async driver.
    """

    def __init__(self, db_path: Path | str | None = None) -> None:
        if isinstance(db_path, Path):
            self._db_path: Path = db_path
        elif db_path is not None:
            self._db_path = Path(db_path)
        else:
            self._db_path = resolve_db_path()
        self._lock = asyncio.Lock()
        self._initialised = False

    @property
    def db_path(self) -> Path:
        return self._db_path

    # ------------------------------------------------------------------
    # Connection / schema
    # ------------------------------------------------------------------

    def _connect_sync(self) -> sqlite3.Connection:
        # ``str(Path(":memory:"))`` is just ``":memory:"`` — works fine.
        # For real paths, ensure the parent directory exists.
        if str(self._db_path) != ":memory:":
            parent = self._db_path.parent
            if parent and not parent.exists():
                parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            str(self._db_path),
            detect_types=sqlite3.PARSE_DECLTYPES,
            isolation_level=None,  # autocommit; we use BEGIN explicitly when needed.
            timeout=30.0,
        )
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    def _ensure_schema_sync(self, conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS fleet_clients (
                repo TEXT PRIMARY KEY,
                caretaker_version TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                first_seen TEXT NOT NULL,
                last_mode TEXT NOT NULL,
                enabled_agents TEXT NOT NULL,
                last_goal_health REAL,
                last_error_count INTEGER NOT NULL DEFAULT 0,
                last_counters TEXT NOT NULL,
                last_summary TEXT,
                heartbeats_seen INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS fleet_heartbeats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                repo TEXT NOT NULL,
                run_at TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_fleet_heartbeats_repo_id
                ON fleet_heartbeats(repo, id);
            """
        )

    async def _ensure_initialised(self) -> None:
        if self._initialised:
            return
        await asyncio.to_thread(self._init_sync)
        self._initialised = True

    def _init_sync(self) -> None:
        conn = self._connect_sync()
        try:
            self._ensure_schema_sync(conn)
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Helpers (sync; called from to_thread)
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_client(row: sqlite3.Row | tuple[Any, ...]) -> FleetClient:
        (
            repo,
            caretaker_version,
            last_seen,
            first_seen,
            last_mode,
            enabled_agents_json,
            last_goal_health,
            last_error_count,
            last_counters_json,
            last_summary_json,
            heartbeats_seen,
        ) = row
        return FleetClient(
            repo=repo,
            caretaker_version=caretaker_version,
            last_seen=_parse_iso(last_seen),
            first_seen=_parse_iso(first_seen),
            last_mode=last_mode,
            enabled_agents=list(json.loads(enabled_agents_json or "[]")),
            last_goal_health=last_goal_health,
            last_error_count=int(last_error_count or 0),
            last_counters=dict(json.loads(last_counters_json or "{}")),
            last_summary=(json.loads(last_summary_json) if last_summary_json else None),
            heartbeats_seen=int(heartbeats_seen or 0),
        )

    def _record_heartbeat_sync(self, payload: dict[str, Any]) -> FleetClient:
        repo = str(payload.get("repo") or "").strip()
        if not repo:
            raise ValueError("heartbeat missing 'repo'")
        now = datetime.now(UTC)
        run_at_raw = payload.get("run_at")
        try:
            run_at = (
                datetime.fromisoformat(run_at_raw.replace("Z", "+00:00"))
                if isinstance(run_at_raw, str)
                else now
            )
        except ValueError:
            run_at = now
        if run_at.tzinfo is None:
            run_at = run_at.replace(tzinfo=UTC)

        conn = self._connect_sync()
        try:
            conn.execute("BEGIN")
            cur = conn.execute(
                "SELECT first_seen, heartbeats_seen FROM fleet_clients WHERE repo = ?",
                (repo,),
            )
            existing = cur.fetchone()
            first_seen = _parse_iso(existing[0]) if existing else run_at
            heartbeats_seen = int(existing[1]) + 1 if existing else 1

            caretaker_version = str(payload.get("caretaker_version", "unknown"))
            last_mode = str(payload.get("mode", "full"))
            enabled_agents = list(payload.get("enabled_agents") or [])
            last_goal_health = payload.get("goal_health")
            last_error_count = int(payload.get("error_count") or 0)
            last_counters = dict(payload.get("counters") or {})
            last_summary = payload.get("summary")

            conn.execute(
                """
                INSERT INTO fleet_clients (
                    repo, caretaker_version, last_seen, first_seen, last_mode,
                    enabled_agents, last_goal_health, last_error_count,
                    last_counters, last_summary, heartbeats_seen
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(repo) DO UPDATE SET
                    caretaker_version = excluded.caretaker_version,
                    last_seen = excluded.last_seen,
                    last_mode = excluded.last_mode,
                    enabled_agents = excluded.enabled_agents,
                    last_goal_health = excluded.last_goal_health,
                    last_error_count = excluded.last_error_count,
                    last_counters = excluded.last_counters,
                    last_summary = excluded.last_summary,
                    heartbeats_seen = excluded.heartbeats_seen
                """,
                (
                    repo,
                    caretaker_version,
                    run_at.isoformat(),
                    first_seen.isoformat(),
                    last_mode,
                    json.dumps(enabled_agents),
                    last_goal_health,
                    last_error_count,
                    json.dumps(last_counters),
                    json.dumps(last_summary) if last_summary is not None else None,
                    heartbeats_seen,
                ),
            )

            snapshot: dict[str, Any] = {
                "repo": repo,
                "caretaker_version": caretaker_version,
                "run_at": run_at.isoformat(),
                "mode": last_mode,
                "enabled_agents": list(enabled_agents),
                "goal_health": last_goal_health,
                "error_count": last_error_count,
                "counters": dict(last_counters),
                "summary": last_summary,
            }
            conn.execute(
                "INSERT INTO fleet_heartbeats(repo, run_at, payload) VALUES (?, ?, ?)",
                (repo, run_at.isoformat(), json.dumps(snapshot)),
            )

            # Prune to ring-buffer length: keep the newest N rows for this repo.
            conn.execute(
                """
                DELETE FROM fleet_heartbeats
                WHERE repo = ?
                  AND id NOT IN (
                    SELECT id FROM fleet_heartbeats
                    WHERE repo = ?
                    ORDER BY id DESC
                    LIMIT ?
                  )
                """,
                (repo, repo, _HEARTBEAT_HISTORY_MAXLEN),
            )

            conn.execute("COMMIT")

            cur = conn.execute(
                """
                SELECT repo, caretaker_version, last_seen, first_seen, last_mode,
                       enabled_agents, last_goal_health, last_error_count,
                       last_counters, last_summary, heartbeats_seen
                FROM fleet_clients WHERE repo = ?
                """,
                (repo,),
            )
            row = cur.fetchone()
            if row is None:  # defensive — we just inserted it
                raise RuntimeError(f"failed to read back fleet client {repo}")
            return self._row_to_client(row)
        except Exception:
            with contextlib.suppress(sqlite3.OperationalError):
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def _recent_heartbeats_sync(self, repo: str, limit: int | None) -> list[dict[str, Any]]:
        conn = self._connect_sync()
        try:
            if limit is None or limit < 0:
                fetch = _HEARTBEAT_HISTORY_MAXLEN
            else:
                fetch = max(0, min(limit, _HEARTBEAT_HISTORY_MAXLEN))
            cur = conn.execute(
                """
                SELECT payload FROM fleet_heartbeats
                WHERE repo = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (repo, fetch),
            )
            rows = cur.fetchall()
        finally:
            conn.close()
        items: list[dict[str, Any]] = []
        # Rows came in DESC; flip to oldest-first to match in-memory store.
        for (payload_json,) in reversed(rows):
            try:
                snapshot = json.loads(payload_json)
            except (TypeError, ValueError):
                continue
            run_at = snapshot.get("run_at")
            if isinstance(run_at, str):
                snapshot["run_at"] = _parse_iso(run_at)
            items.append(snapshot)
        return items

    def _list_clients_sync(self) -> list[FleetClient]:
        conn = self._connect_sync()
        try:
            cur = conn.execute(
                """
                SELECT repo, caretaker_version, last_seen, first_seen, last_mode,
                       enabled_agents, last_goal_health, last_error_count,
                       last_counters, last_summary, heartbeats_seen
                FROM fleet_clients
                ORDER BY last_seen DESC
                """
            )
            rows = cur.fetchall()
        finally:
            conn.close()
        return [self._row_to_client(r) for r in rows]

    def _get_client_sync(self, repo: str) -> FleetClient | None:
        conn = self._connect_sync()
        try:
            cur = conn.execute(
                """
                SELECT repo, caretaker_version, last_seen, first_seen, last_mode,
                       enabled_agents, last_goal_health, last_error_count,
                       last_counters, last_summary, heartbeats_seen
                FROM fleet_clients WHERE repo = ?
                """,
                (repo,),
            )
            row = cur.fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        return self._row_to_client(row)

    def _remove_client_sync(self, repo: str) -> bool:
        conn = self._connect_sync()
        try:
            conn.execute("BEGIN")
            cur = conn.execute("DELETE FROM fleet_clients WHERE repo = ?", (repo,))
            removed = cur.rowcount > 0
            conn.execute("DELETE FROM fleet_heartbeats WHERE repo = ?", (repo,))
            conn.execute("COMMIT")
            return removed
        except Exception:
            with contextlib.suppress(sqlite3.OperationalError):
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def _size_sync(self) -> int:
        conn = self._connect_sync()
        try:
            cur = conn.execute("SELECT COUNT(*) FROM fleet_clients")
            (n,) = cur.fetchone()
        finally:
            conn.close()
        return int(n)

    def _stale_clients_sync(self, threshold: timedelta) -> list[FleetClient]:
        cutoff = datetime.now(UTC) - threshold
        conn = self._connect_sync()
        try:
            cur = conn.execute(
                """
                SELECT repo, caretaker_version, last_seen, first_seen, last_mode,
                       enabled_agents, last_goal_health, last_error_count,
                       last_counters, last_summary, heartbeats_seen
                FROM fleet_clients
                WHERE last_seen < ?
                ORDER BY last_seen ASC
                """,
                (cutoff.isoformat(),),
            )
            rows = cur.fetchall()
        finally:
            conn.close()
        return [self._row_to_client(r) for r in rows]

    # ------------------------------------------------------------------
    # Async API (mirrors FleetRegistryStore)
    # ------------------------------------------------------------------

    async def record_heartbeat(self, payload: dict[str, Any]) -> FleetClient:
        await self._ensure_initialised()
        async with self._lock:
            return await asyncio.to_thread(self._record_heartbeat_sync, payload)

    async def recent_heartbeats(
        self, repo: str, *, limit: int | None = None
    ) -> list[dict[str, Any]]:
        await self._ensure_initialised()
        async with self._lock:
            return await asyncio.to_thread(self._recent_heartbeats_sync, repo, limit)

    async def list_clients(self) -> list[FleetClient]:
        await self._ensure_initialised()
        async with self._lock:
            return await asyncio.to_thread(self._list_clients_sync)

    async def get_client(self, repo: str) -> FleetClient | None:
        await self._ensure_initialised()
        async with self._lock:
            return await asyncio.to_thread(self._get_client_sync, repo)

    async def remove_client(self, repo: str) -> bool:
        await self._ensure_initialised()
        async with self._lock:
            return await asyncio.to_thread(self._remove_client_sync, repo)

    async def size(self) -> int:
        await self._ensure_initialised()
        async with self._lock:
            return await asyncio.to_thread(self._size_sync)

    async def stale_clients(self, *, threshold: timedelta) -> list[FleetClient]:
        await self._ensure_initialised()
        async with self._lock:
            return await asyncio.to_thread(self._stale_clients_sync, threshold)


def _parse_iso(value: str) -> datetime:
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return datetime.now(UTC)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


__all__: Iterable[str] = (
    "SQLiteFleetRegistryStore",
    "resolve_db_path",
    "DEFAULT_DB_PATH_ENV",
)
