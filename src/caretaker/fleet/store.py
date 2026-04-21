"""In-memory fleet-registry store.

A simple dict keyed by repo slug. Replaceable with a persistent
backend later (SQLite, Mongo) without changing the API surface —
``FleetRegistryStore`` is the one seam the endpoints and admin API
depend on.
"""

from __future__ import annotations

import asyncio
import logging
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


class FleetRegistryStore:
    """Async-safe in-memory fleet registry."""

    def __init__(self) -> None:
        self._clients: dict[str, FleetClient] = {}
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
            return record

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
_STORE = FleetRegistryStore()


def get_store() -> FleetRegistryStore:
    return _STORE


def reset_store_for_tests() -> None:
    """Test helper — replace the module singleton with a fresh store."""
    global _STORE  # noqa: PLW0603
    _STORE = FleetRegistryStore()
