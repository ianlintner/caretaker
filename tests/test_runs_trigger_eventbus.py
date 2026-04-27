"""run_trigger publishes onto the event bus when one is configured.

Verifies the new durable-dispatch path for ``/runs/{id}/trigger``: when
``event_bus_factory`` is wired in, ``run_trigger()`` publishes a
``run_trigger`` payload onto ``caretaker:events`` rather than spawning
an in-process asyncio task. Falls back gracefully on bus failure.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from caretaker.eventbus import LocalEventBus
from caretaker.eventbus.base import EventBusError
from caretaker.runs import dispatch as runs_dispatch
from caretaker.runs.models import RunRecord, RunStatus, RunTriggerRequest


class _FakeDispatcher:
    """Minimal stand-in for WebhookDispatcher; never reached in bus-success path."""

    class _Mode:
        value = "active"

    mode = _Mode()

    async def dispatch(self, parsed: Any) -> Any:  # pragma: no cover
        raise AssertionError("dispatcher should not be called when publish succeeds")


class _FakeResolver:
    async def get(self, repo: str) -> int:
        return 99


class _FakeTokenBroker:
    pass


def _record() -> RunRecord:
    return RunRecord(
        run_id="run-1",
        repository="acme/demo",
        repository_id=42,
        repository_owner="acme",
        gh_run_id=1,
        gh_run_attempt=1,
        actor="someone",
        event_name="schedule",
        workflow="Caretaker",
        ref="refs/heads/main",
        sha="abc",
        mode="full",
        status=RunStatus.PENDING,
        started_at=datetime.now(UTC),
        last_seq=7,
    )


@pytest.fixture(autouse=True)
def _reset() -> None:
    runs_dispatch.reset()
    yield
    runs_dispatch.reset()


@pytest.mark.asyncio
async def test_run_trigger_publishes_to_event_bus_when_configured() -> None:
    bus = LocalEventBus()
    runs_dispatch.configure(
        resolver=_FakeResolver(),  # type: ignore[arg-type]
        token_broker=_FakeTokenBroker(),  # type: ignore[arg-type]
        dispatcher_factory=lambda: _FakeDispatcher(),
        event_bus_factory=lambda: bus,
    )

    body = RunTriggerRequest(mode="full")
    ok = await runs_dispatch.run_trigger(_record(), body)
    assert ok is True

    state = bus._streams.get("caretaker:events")  # type: ignore[attr-defined]
    assert state is not None
    assert len(state.queue) == 1
    _id, payload = state.queue[0]
    assert payload["kind"] == "run_trigger"
    assert payload["run_id"] == "run-1"
    assert payload["last_seq"] == 7
    assert payload["repository_full_name"] == "acme/demo"
    assert payload["installation_id"] == 99


@pytest.mark.asyncio
async def test_run_trigger_falls_back_to_inprocess_when_publish_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bus outage → in-process asyncio task path; trigger still returns True."""

    class _BrokenBus(LocalEventBus):
        async def publish(self, stream: str, payload: dict) -> str:  # type: ignore[override]
            raise EventBusError("simulated outage")

    runs_dispatch.configure(
        resolver=_FakeResolver(),  # type: ignore[arg-type]
        token_broker=_FakeTokenBroker(),  # type: ignore[arg-type]
        dispatcher_factory=lambda: _FakeDispatcher(),
        event_bus_factory=lambda: _BrokenBus(),
    )

    # In-process path will try to call dispatcher.dispatch — patch our
    # fake to a no-op stub for this case so the asyncio task can complete
    # without exercising the runs store.
    class _StubDispatcher(_FakeDispatcher):
        async def dispatch(self, parsed: Any) -> Any:
            return None

    runs_dispatch._dispatcher_factory = lambda: _StubDispatcher()  # type: ignore[assignment]

    # Stub get_store so the in-process runner doesn't blow up writing terminal status.
    class _StubStore:
        async def update_run(self, *args: Any, **kwargs: Any) -> None:
            return None

        async def get_run(self, run_id: str) -> Any:
            return None

        async def append_log(self, *args: Any, **kwargs: Any) -> None:
            return None

    monkeypatch.setattr(runs_dispatch, "get_store", lambda: _StubStore())

    body = RunTriggerRequest(mode="full")
    ok = await runs_dispatch.run_trigger(_record(), body)
    # Trigger still considered successful — fallback path scheduled the task.
    assert ok is True
