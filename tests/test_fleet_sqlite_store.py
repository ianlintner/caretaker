"""Tests for the SQLite-backed fleet registry store.

These exercise the same async API as the in-memory ``FleetRegistryStore``
plus two SQLite-specific concerns:

* The data survives store re-creation (durability across simulated
  process restarts).
* ``CARETAKER_FLEET_DB_PATH`` is honoured by ``resolve_db_path`` and by
  the lazy-init seam in ``store.get_store``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from caretaker.fleet import store as store_module
from caretaker.fleet.sqlite_store import (
    DEFAULT_DB_PATH_ENV,
    SQLiteFleetRegistryStore,
    resolve_db_path,
)

# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


def test_resolve_db_path_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(DEFAULT_DB_PATH_ENV, raising=False)
    path = resolve_db_path()
    assert path == Path.home() / ".local/state/caretaker/fleet-registry.db"


def test_resolve_db_path_honours_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    custom = tmp_path / "custom.db"
    monkeypatch.setenv(DEFAULT_DB_PATH_ENV, str(custom))
    assert resolve_db_path() == custom


def test_resolve_db_path_honours_explicit_argument() -> None:
    assert resolve_db_path(":memory:") == Path(":memory:")


def test_resolve_db_path_expands_user(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(DEFAULT_DB_PATH_ENV, raising=False)
    path = resolve_db_path("~/somewhere/fleet.db")
    assert path == Path.home() / "somewhere/fleet.db"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _as_datetime(value: object) -> datetime:
    """Normalise a heartbeat ``run_at`` value to ``datetime`` for ordering checks."""
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    raise TypeError(f"unexpected run_at type: {type(value).__name__}")


def _payload(
    repo: str = "ianlintner/example",
    *,
    run_at: str | None = None,
    goal_health: float | None = 0.85,
    error_count: int = 0,
    counters: dict[str, int] | None = None,
    enabled_agents: list[str] | None = None,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "repo": repo,
        "caretaker_version": "0.20.0",
        "run_at": run_at or datetime.now(UTC).isoformat(),
        "mode": "full",
        "enabled_agents": enabled_agents or ["pr_agent", "issue_agent"],
        "goal_health": goal_health,
        "error_count": error_count,
        "counters": counters or {"prs_processed": 3, "issues_triaged": 1},
        "summary": None,
        "attribution": None,
    }


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_heartbeat_and_get_client(tmp_path: Path) -> None:
    store = SQLiteFleetRegistryStore(tmp_path / "fleet.db")
    client = await store.record_heartbeat(_payload())
    assert client.repo == "ianlintner/example"
    assert client.heartbeats_seen == 1
    assert client.caretaker_version == "0.20.0"

    fetched = await store.get_client("ianlintner/example")
    assert fetched is not None
    assert fetched.heartbeats_seen == 1
    assert fetched.last_counters == {"prs_processed": 3, "issues_triaged": 1}
    assert fetched.enabled_agents == ["pr_agent", "issue_agent"]


@pytest.mark.asyncio
async def test_repeated_heartbeats_increment_counter(tmp_path: Path) -> None:
    store = SQLiteFleetRegistryStore(tmp_path / "fleet.db")
    for _ in range(3):
        await store.record_heartbeat(_payload())
    fetched = await store.get_client("ianlintner/example")
    assert fetched is not None
    assert fetched.heartbeats_seen == 3


@pytest.mark.asyncio
async def test_recent_heartbeats_returns_oldest_first(tmp_path: Path) -> None:
    store = SQLiteFleetRegistryStore(tmp_path / "fleet.db")
    base = datetime(2026, 1, 1, tzinfo=UTC)
    for i in range(5):
        await store.record_heartbeat(
            _payload(run_at=(base + timedelta(minutes=i)).isoformat())
        )
    history = await store.recent_heartbeats("ianlintner/example")
    assert len(history) == 5
    run_ats = [h["run_at"] for h in history]
    assert run_ats == sorted(run_ats)


@pytest.mark.asyncio
async def test_recent_heartbeats_respects_limit(tmp_path: Path) -> None:
    store = SQLiteFleetRegistryStore(tmp_path / "fleet.db")
    for i in range(5):
        await store.record_heartbeat(_payload(run_at=f"2026-01-0{i + 1}T00:00:00Z"))
    history = await store.recent_heartbeats("ianlintner/example", limit=2)
    assert len(history) == 2
    # Oldest-first within the requested window (most recent two)
    assert history[0]["run_at"] < history[1]["run_at"]


@pytest.mark.asyncio
async def test_history_pruned_to_max_length(tmp_path: Path) -> None:
    """Match the in-memory ``deque(maxlen=32)`` semantics."""
    store = SQLiteFleetRegistryStore(tmp_path / "fleet.db")
    base = datetime(2026, 1, 1, tzinfo=UTC)
    for i in range(40):
        await store.record_heartbeat(
            _payload(run_at=(base + timedelta(minutes=i)).isoformat())
        )
    history = await store.recent_heartbeats("ianlintner/example")
    assert len(history) == 32
    # Pruning keeps the most recent 32 entries (oldest 8 dropped)
    earliest_kept = history[0]["run_at"]
    assert _as_datetime(earliest_kept) >= base + timedelta(minutes=8)


@pytest.mark.asyncio
async def test_list_clients_sorted_by_last_seen_desc(tmp_path: Path) -> None:
    store = SQLiteFleetRegistryStore(tmp_path / "fleet.db")
    await store.record_heartbeat(_payload("a/one", run_at="2026-01-01T00:00:00Z"))
    await store.record_heartbeat(_payload("a/two", run_at="2026-01-02T00:00:00Z"))
    await store.record_heartbeat(_payload("a/three", run_at="2026-01-03T00:00:00Z"))
    clients = await store.list_clients()
    assert [c.repo for c in clients] == ["a/three", "a/two", "a/one"]


@pytest.mark.asyncio
async def test_remove_client(tmp_path: Path) -> None:
    store = SQLiteFleetRegistryStore(tmp_path / "fleet.db")
    await store.record_heartbeat(_payload("a/one"))
    await store.record_heartbeat(_payload("a/two"))
    assert await store.size() == 2
    assert await store.remove_client("a/one") is True
    assert await store.size() == 1
    assert await store.remove_client("a/one") is False
    # Heartbeat history for the removed repo is also cleared
    assert await store.recent_heartbeats("a/one") == []


@pytest.mark.asyncio
async def test_stale_clients(tmp_path: Path) -> None:
    store = SQLiteFleetRegistryStore(tmp_path / "fleet.db")
    fresh = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    stale = (datetime.now(UTC) - timedelta(days=10)).isoformat()
    await store.record_heartbeat(_payload("a/fresh", run_at=fresh))
    await store.record_heartbeat(_payload("a/stale", run_at=stale))
    stale_clients = await store.stale_clients(threshold=timedelta(days=7))
    assert [c.repo for c in stale_clients] == ["a/stale"]


# ---------------------------------------------------------------------------
# Durability across instances
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_state_survives_store_reopen(tmp_path: Path) -> None:
    db_path = tmp_path / "fleet.db"
    first = SQLiteFleetRegistryStore(db_path)
    await first.record_heartbeat(_payload("a/one", run_at="2026-01-01T00:00:00Z"))
    await first.record_heartbeat(_payload("a/one", run_at="2026-01-02T00:00:00Z"))
    await first.record_heartbeat(_payload("a/two", run_at="2026-01-03T00:00:00Z"))

    second = SQLiteFleetRegistryStore(db_path)
    assert await second.size() == 2
    one = await second.get_client("a/one")
    assert one is not None
    assert one.heartbeats_seen == 2
    history = await second.recent_heartbeats("a/one")
    assert [h["run_at"] for h in history] == [
        datetime(2026, 1, 1, tzinfo=UTC),
        datetime(2026, 1, 2, tzinfo=UTC),
    ]


# ---------------------------------------------------------------------------
# get_store seam
# ---------------------------------------------------------------------------


def test_get_store_uses_sqlite_when_env_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db_path = tmp_path / "fleet.db"
    monkeypatch.setenv(DEFAULT_DB_PATH_ENV, str(db_path))
    # Force re-init by clearing the cached singleton
    store_module._STORE_INITIALISED = False  # type: ignore[attr-defined]
    store_module._STORE = store_module.FleetRegistryStore()  # type: ignore[attr-defined]

    try:
        store = store_module.get_store()
        assert isinstance(store, SQLiteFleetRegistryStore)
    finally:
        # Reset for the rest of the test session
        store_module._STORE_INITIALISED = False  # type: ignore[attr-defined]
        store_module._STORE = store_module.FleetRegistryStore()  # type: ignore[attr-defined]


def test_get_store_defaults_to_in_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(DEFAULT_DB_PATH_ENV, raising=False)
    store_module._STORE_INITIALISED = False  # type: ignore[attr-defined]
    store_module._STORE = store_module.FleetRegistryStore()  # type: ignore[attr-defined]

    try:
        store = store_module.get_store()
        assert isinstance(store, store_module.FleetRegistryStore)
    finally:
        store_module._STORE_INITIALISED = False  # type: ignore[attr-defined]
        store_module._STORE = store_module.FleetRegistryStore()  # type: ignore[attr-defined]
