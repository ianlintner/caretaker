"""Fleet-registry store seam.

The default :class:`FleetRegistryStore` implementation is an
in-memory async-safe dict keyed by repo slug. The companion
:mod:`caretaker.fleet.sqlite_store` module provides a durable
SQLite-backed implementation with the same async API; selection is
controlled by the ``CARETAKER_FLEET_DB_PATH`` environment variable
through :func:`get_store`. Endpoints and the admin API only depend on
the duck-typed async surface (``record_heartbeat``, ``recent_heartbeats``,
``list_clients``, ``get_client``, ``remove_client``, ``size``,
``stale_clients``).
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class FleetClient:
    """Represents one known consumer repo.

    ``heartbeats_seen`` is monotonically incremented on every accepted
    heartbeat; ``last_seen`` is the wall-clock of the most recent one.
    """

    repo: str
    caretaker_version: str
    last_seen: datetime
    first_seen: datetime
    last_mode: str = "full"
    enabled_agents: list[str] = field(default_factory=list)
    last_goal_health: float | None = None
    last_error_count: int = 0
    last_counters: dict[str, int] = field(default_factory=dict)
    last_summary: dict[str, Any] | None = None
    heartbeats_seen: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo": self.repo,
            "caretaker_version": self.caretaker_version,
            "last_seen": self.last_seen.isoformat(),
            "first_seen": self.first_seen.isoformat(),
            "last_mode": self.last_mode,
            "enabled_agents": list(self.enabled_agents),
            "last_goal_health": self.last_goal_health,
            "last_error_count": self.last_error_count,
            "last_counters": dict(self.last_counters),
            "last_summary": self.last_summary,
            "heartbeats_seen": self.heartbeats_seen,
        }


#: Ring-buffer cap for per-repo heartbeat history. Alerts only ever look at the
#: most recent handful of heartbeats (N consecutive goal-health dips, error
#: spike vs. prior mean), so a short bounded deque is plenty and keeps the
#: store memory O(#repos).
_HEARTBEAT_HISTORY_MAXLEN = 32


class FleetRegistryStore:
    """Async-safe in-memory fleet registry."""

    def __init__(self) -> None:
        self._clients: dict[str, FleetClient] = {}
        self._history: dict[str, deque[dict[str, Any]]] = {}
        self._lock = asyncio.Lock()

    async def record_heartbeat(self, payload: dict[str, Any]) -> FleetClient:
        """Persist (or refresh) a client from a raw heartbeat payload.

        Returns the stored record.
        """
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

        async with self._lock:
            existing = self._clients.get(repo)
            record = FleetClient(
                repo=repo,
                caretaker_version=str(payload.get("caretaker_version", "unknown")),
                last_seen=run_at,
                first_seen=existing.first_seen if existing else run_at,
                last_mode=str(payload.get("mode", "full")),
                enabled_agents=list(payload.get("enabled_agents") or []),
                last_goal_health=payload.get("goal_health"),
                last_error_count=int(payload.get("error_count") or 0),
                last_counters=dict(payload.get("counters") or {}),
                last_summary=payload.get("summary"),
                heartbeats_seen=(existing.heartbeats_seen if existing else 0) + 1,
            )
            self._clients[repo] = record
            history = self._history.setdefault(repo, deque(maxlen=_HEARTBEAT_HISTORY_MAXLEN))
            # Keep a copy so later mutation of ``payload`` by the caller
            # can't corrupt the history record.
            snapshot: dict[str, Any] = {
                "repo": repo,
                "caretaker_version": record.caretaker_version,
                "run_at": run_at,
                "mode": record.last_mode,
                "enabled_agents": list(record.enabled_agents),
                "goal_health": record.last_goal_health,
                "error_count": record.last_error_count,
                "counters": dict(record.last_counters),
                "summary": record.last_summary,
            }
            history.append(snapshot)
            return record

    async def recent_heartbeats(
        self, repo: str, *, limit: int | None = None
    ) -> list[dict[str, Any]]:
        """Return the bounded heartbeat ring buffer for ``repo``, oldest-first.

        Each entry is a dict with the same shape the emitter sends, normalised
        (``run_at`` is a ``datetime`` with tzinfo). Used by the alert evaluator
        so alerts can reason about N consecutive heartbeats without touching
        every caller that already has just the last one.
        """
        async with self._lock:
            buf = self._history.get(repo)
            if not buf:
                return []
            items = list(buf)
        if limit is not None and limit >= 0:
            items = items[-limit:]
        return items

    async def list_clients(self) -> list[FleetClient]:
        async with self._lock:
            return sorted(self._clients.values(), key=lambda c: c.last_seen, reverse=True)

    async def get_client(self, repo: str) -> FleetClient | None:
        async with self._lock:
            return self._clients.get(repo)

    async def remove_client(self, repo: str) -> bool:
        async with self._lock:
            return self._clients.pop(repo, None) is not None

    async def size(self) -> int:
        async with self._lock:
            return len(self._clients)

    async def stale_clients(self, *, threshold: timedelta) -> list[FleetClient]:
        cutoff = datetime.now(UTC) - threshold
        async with self._lock:
            return [c for c in self._clients.values() if c.last_seen < cutoff]


# Module-level singleton. Tests can monkey-patch this or call reset().
# Type is kept loose because the SQLite-backed implementation lives in a
# sibling module and intentionally has the same async surface without a
# shared base class.
_STORE: Any = FleetRegistryStore()
_STORE_INITIALISED = False

_FLEET_DB_PATH_ENV = "CARETAKER_FLEET_DB_PATH"


def _build_store() -> Any:
    """Construct the appropriate store from the environment.

    When ``CARETAKER_FLEET_DB_PATH`` is set, returns a SQLite-backed store
    rooted at that path (``:memory:`` is honoured for tests). Otherwise
    falls back to the in-memory implementation, which preserves backwards
    compatibility for unit tests and lightweight deployments.
    """
    db_path = os.environ.get(_FLEET_DB_PATH_ENV)
    if db_path and db_path.strip():
        try:
            from .sqlite_store import SQLiteFleetRegistryStore  # local import to avoid cycle
        except ImportError:  # pragma: no cover — sqlite is in stdlib
            logger.warning("SQLite fleet store unavailable; falling back to in-memory")
            return FleetRegistryStore()
        try:
            return SQLiteFleetRegistryStore(db_path.strip())
        except Exception:  # noqa: BLE001
            logger.exception(
                "Failed to initialise SQLite fleet store at %s; falling back to in-memory",
                db_path,
            )
            return FleetRegistryStore()
    return FleetRegistryStore()


def get_store() -> Any:
    """Return the active fleet registry store.

    The first call honours ``CARETAKER_FLEET_DB_PATH``. After that the
    selected backend is cached for the process lifetime; tests can swap
    it via :func:`reset_store_for_tests` (with optional explicit store).
    """
    global _STORE, _STORE_INITIALISED  # noqa: PLW0603
    if not _STORE_INITIALISED:
        _STORE = _build_store()
        _STORE_INITIALISED = True
    return _STORE


def reset_store_for_tests(store: Any | None = None) -> None:
    """Test helper — replace the module singleton with a fresh store.

    Pass ``store`` to inject a custom backend (e.g. an in-memory SQLite
    store for persistence tests). When omitted, a fresh in-memory store
    is used so legacy tests keep working without touching the env.
    """
    global _STORE, _STORE_INITIALISED  # noqa: PLW0603
    _STORE = store if store is not None else FleetRegistryStore()
    _STORE_INITIALISED = True
