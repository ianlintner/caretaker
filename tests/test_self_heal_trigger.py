"""Tests for the backend-side self-heal trigger."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from caretaker.eventbus import LocalEventBus
from caretaker.runs.models import RunRecord, RunStatus
from caretaker.runs.self_heal_trigger import publish_self_heal_trigger


def _record(*, gh_run_id: int = 1) -> RunRecord:
    return RunRecord(
        run_id="run-self-heal-1",
        repository="acme/demo",
        repository_id=42,
        repository_owner="acme",
        gh_run_id=gh_run_id,
        gh_run_attempt=1,
        actor="someone",
        event_name="schedule",
        ref="refs/heads/main",
        sha="abc",
        mode="full",
        status=RunStatus.FAILED,
        started_at=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_publish_self_heal_trigger_emits_workflow_run_event() -> None:
    bus = LocalEventBus()
    await publish_self_heal_trigger(
        bus=bus, record=_record(gh_run_id=12345), exit_code=1, summary={"error": "boom"}
    )
    state = bus._streams["caretaker:events"]  # type: ignore[attr-defined]
    assert len(state.queue) == 1
    _id, payload = state.queue[0]
    assert payload["kind"] == "webhook"
    assert payload["event_type"] == "workflow_run"
    assert payload["delivery_id"] == "self-heal:run-self-heal-1"
    assert payload["repository_full_name"] == "acme/demo"
    raw = payload["raw_payload"]
    assert raw["workflow_run"]["conclusion"] == "failure"
    assert raw["workflow_run"]["exit_code"] == 1
    assert raw["workflow_run"]["run_id"] == "run-self-heal-1"


@pytest.mark.asyncio
async def test_publish_self_heal_trigger_no_op_on_success() -> None:
    bus = LocalEventBus()
    await publish_self_heal_trigger(bus=bus, record=_record(), exit_code=0, summary=None)
    assert "caretaker:events" not in bus._streams  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_publish_self_heal_trigger_swallows_bus_errors() -> None:
    """Publish failure must not raise — self-heal is best-effort."""

    class _BrokenBus(LocalEventBus):
        async def publish(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            raise RuntimeError("bus down")

    # Should not raise.
    await publish_self_heal_trigger(bus=_BrokenBus(), record=_record(), exit_code=1, summary={})
