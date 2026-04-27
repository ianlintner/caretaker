"""Tests for the backend-side reconciliation scheduler.

Verifies fan-out semantics: one ``schedule`` event published per
installed repo, lease serialises ticks across replicas, and bus failures
are non-fatal.
"""

from __future__ import annotations

from typing import Any

import pytest

from caretaker.eventbus import LocalEventBus
from caretaker.eventbus.base import EventBusError
from caretaker.github_app.installations_index import FleetRepo
from caretaker.scheduler.reconciliation import ReconciliationScheduler


class _StubIndex:
    """Stand-in for :class:`InstallationsIndex` returning a fixed fleet."""

    def __init__(self, repos: list[FleetRepo]) -> None:
        self._repos = repos

    async def list_repos(self, *, force_refresh: bool = False) -> list[FleetRepo]:
        return list(self._repos)


def _repos() -> list[FleetRepo]:
    return [
        FleetRepo(owner="acme", repo="alpha", installation_id=1),
        FleetRepo(owner="acme", repo="beta", installation_id=1),
        FleetRepo(owner="globex", repo="gamma", installation_id=2),
    ]


@pytest.mark.asyncio
async def test_tick_publishes_one_event_per_repo() -> None:
    bus = LocalEventBus()
    sched = ReconciliationScheduler(
        bus=bus,
        installations_index=_StubIndex(_repos()),  # type: ignore[arg-type]
        redis_url="",  # no Redis → always-on lease
    )

    published = await sched.tick()
    assert published == 3

    state = bus._streams["caretaker:events"]  # type: ignore[attr-defined]
    repos_seen = [payload["repository_full_name"] for _id, payload in state.queue]
    assert sorted(repos_seen) == ["acme/alpha", "acme/beta", "globex/gamma"]
    for _id, payload in state.queue:
        assert payload["event_type"] == "schedule"
        assert payload["delivery_id"].startswith("scheduler:")
        assert payload["installation_id"] in {1, 2}


@pytest.mark.asyncio
async def test_tick_skips_when_lease_held_by_other_replica() -> None:
    """When ``_try_acquire_lease`` returns False, no events publish."""
    bus = LocalEventBus()
    sched = ReconciliationScheduler(
        bus=bus,
        installations_index=_StubIndex(_repos()),  # type: ignore[arg-type]
        redis_url="redis://stub",  # forces lease path
    )

    async def _no_lease() -> bool:
        return False

    sched._try_acquire_lease = _no_lease  # type: ignore[method-assign]

    published = await sched.tick()
    assert published == 0
    state = bus._streams.get("caretaker:events")  # type: ignore[attr-defined]
    assert state is None or len(state.queue) == 0


@pytest.mark.asyncio
async def test_tick_continues_after_per_repo_publish_failure() -> None:
    """A bus failure on one repo should not stop fan-out on the others."""

    class _FlakyBus(LocalEventBus):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        async def publish(self, stream: str, payload: dict[str, Any]) -> str:  # type: ignore[override]
            self.calls += 1
            if self.calls == 2:
                raise EventBusError("simulated outage on second publish")
            return await super().publish(stream, payload)

    bus = _FlakyBus()
    sched = ReconciliationScheduler(
        bus=bus,
        installations_index=_StubIndex(_repos()),  # type: ignore[arg-type]
        redis_url="",
    )

    published = await sched.tick()
    # Three repos attempted, one failed → two delivered.
    assert published == 2
    assert bus.calls == 3


@pytest.mark.asyncio
async def test_tick_returns_zero_when_no_repos() -> None:
    bus = LocalEventBus()
    sched = ReconciliationScheduler(
        bus=bus,
        installations_index=_StubIndex([]),  # type: ignore[arg-type]
        redis_url="",
    )
    assert await sched.tick() == 0
    assert (
        "caretaker:events" not in bus._streams
        or len(  # type: ignore[attr-defined]
            bus._streams["caretaker:events"].queue  # type: ignore[attr-defined]
        )
        == 0
    )


@pytest.mark.asyncio
async def test_fanout_payload_kind_is_webhook_for_dispatcher_compat() -> None:
    """The dispatcher unpacks ``kind=webhook`` events; scheduler events
    must use the same envelope so a single consumer body handles both."""
    bus = LocalEventBus()
    sched = ReconciliationScheduler(
        bus=bus,
        installations_index=_StubIndex([_repos()[0]]),  # type: ignore[arg-type]
        redis_url="",
    )

    await sched.tick()
    state = bus._streams["caretaker:events"]  # type: ignore[attr-defined]
    _id, payload = state.queue[0]
    assert payload["kind"] == "webhook"
    assert payload["event_type"] == "schedule"
