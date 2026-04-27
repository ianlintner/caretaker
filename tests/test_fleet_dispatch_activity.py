"""Tests for the server-side fleet dispatch-touch (post-v0.25.0 fleet upgrade).

After PR #621 + the v0.25.0 fleet rollout removed ``caretaker run`` from
every consumer-side workflow, no consumer process emits the legacy
``emit_heartbeat`` HTTP POST anymore. The ``/api/admin/fleet`` view
would have gone stale-frozen at the pre-migration snapshot. This test
suite covers the new ``record_dispatch_activity`` helper and confirms
the eventbus consumer wires it on every successful webhook dispatch.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from caretaker.fleet import record_dispatch_activity
from caretaker.fleet.store import FleetRegistryStore, reset_store_for_tests


@pytest.fixture(autouse=True)
def _isolated_fleet_store() -> None:
    """Each test starts with a fresh in-memory fleet store."""
    store = FleetRegistryStore()
    reset_store_for_tests(store)
    yield
    reset_store_for_tests()


@pytest.mark.asyncio
async def test_record_dispatch_activity_writes_minimal_heartbeat() -> None:
    ok = await record_dispatch_activity(
        repo="acme/demo",
        event_type="pull_request",
        agents_fired=["pr", "pr-reviewer"],
        outcome="active",
    )
    assert ok is True

    from caretaker.fleet.store import get_store

    store = get_store()
    client = await store.get_client("acme/demo")
    assert client is not None
    assert client.repo == "acme/demo"
    assert client.last_mode == "webhook:pull_request"
    assert sorted(client.enabled_agents) == ["pr", "pr-reviewer"]
    assert client.last_error_count == 0


@pytest.mark.asyncio
async def test_record_dispatch_activity_marks_error_count_on_failure() -> None:
    await record_dispatch_activity(
        repo="acme/demo",
        event_type="issues",
        agents_fired=["issue"],
        outcome="error",
    )
    from caretaker.fleet.store import get_store

    client = await (get_store()).get_client("acme/demo")
    assert client is not None
    assert client.last_error_count == 1


@pytest.mark.asyncio
async def test_record_dispatch_activity_partial_dispatch_counts_as_error() -> None:
    """`active_partial` (one agent failed but siblings succeeded) is a
    legitimate dispatch failure — the registry should reflect it so the
    admin SPA can highlight repos with degraded agent runs."""
    await record_dispatch_activity(
        repo="acme/demo",
        event_type="pull_request",
        agents_fired=["pr", "pr-reviewer"],
        outcome="active_partial",
    )
    from caretaker.fleet.store import get_store

    client = await (get_store()).get_client("acme/demo")
    assert client is not None
    assert client.last_error_count == 1


@pytest.mark.asyncio
async def test_record_dispatch_activity_shadow_mode_is_clean() -> None:
    """Shadow mode is observation-only — must not look like an error in
    the fleet registry."""
    await record_dispatch_activity(
        repo="acme/demo",
        event_type="pull_request",
        agents_fired=["pr"],
        outcome="shadow",
    )
    from caretaker.fleet.store import get_store

    client = await (get_store()).get_client("acme/demo")
    assert client is not None
    assert client.last_error_count == 0


@pytest.mark.asyncio
async def test_record_dispatch_activity_rejects_invalid_repo() -> None:
    """Mis-shaped slugs are no-ops, not errors."""
    assert await record_dispatch_activity(repo="", event_type="x") is False
    assert await record_dispatch_activity(repo="not-a-slug", event_type="x") is False


@pytest.mark.asyncio
async def test_record_dispatch_activity_swallows_store_errors() -> None:
    """A failing store backend must not propagate into the dispatcher."""

    class _BrokenStore:
        async def record_heartbeat(self, payload: dict[str, Any]) -> Any:
            raise RuntimeError("simulated mongo outage")

    reset_store_for_tests(_BrokenStore())  # type: ignore[arg-type]
    ok = await record_dispatch_activity(repo="acme/demo", event_type="pull_request")
    assert ok is False


@pytest.mark.asyncio
async def test_record_dispatch_activity_increments_heartbeats_seen() -> None:
    """Consecutive dispatches for the same repo upsert the same row."""
    for _ in range(3):
        await record_dispatch_activity(repo="acme/demo", event_type="push")

    from caretaker.fleet.store import get_store

    client = await (get_store()).get_client("acme/demo")
    assert client is not None
    assert client.heartbeats_seen == 3


# ── Eventbus integration ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_consumer_webhook_handler_records_dispatch_activity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The eventbus consumer's ``_handle_webhook`` must call the new
    ``record_dispatch_activity`` after a successful dispatch so the
    fleet registry stays live without the legacy consumer-side emitter."""
    from caretaker.eventbus.consumer import _handle_webhook
    from caretaker.github_app.dispatcher import DispatchMode, DispatchResult
    from caretaker.github_app.webhooks import ParsedWebhook

    parsed = ParsedWebhook(
        event_type="pull_request",
        delivery_id="d-1",
        action="opened",
        installation_id=42,
        repository_full_name="acme/demo",
        payload={},
    )
    dispatcher = AsyncMock()
    dispatcher.mode = DispatchMode.ACTIVE
    dispatcher.dispatch = AsyncMock(
        return_value=DispatchResult(
            mode=DispatchMode.ACTIVE,
            event="pull_request",
            delivery_id="d-1",
            agents=("pr", "pr-reviewer"),
            outcome="active",
            duration_seconds=0.1,
        )
    )

    await _handle_webhook(parsed=parsed, dispatcher=dispatcher)

    # Fleet store should now know about acme/demo.
    from caretaker.fleet.store import get_store

    client = await (get_store()).get_client("acme/demo")
    assert client is not None
    assert client.last_mode == "webhook:pull_request"
    assert sorted(client.enabled_agents) == ["pr", "pr-reviewer"]


@pytest.mark.asyncio
async def test_consumer_webhook_handler_skips_record_when_dispatcher_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When dispatcher.mode is OFF, the consumer should NOT record a
    fleet activity (otherwise mere webhook receipt looks like dispatch
    activity and inflates the fleet view)."""
    from caretaker.eventbus.consumer import _handle_webhook
    from caretaker.github_app.dispatcher import DispatchMode, DispatchResult
    from caretaker.github_app.webhooks import ParsedWebhook

    parsed = ParsedWebhook(
        event_type="pull_request",
        delivery_id="d-1",
        action="opened",
        installation_id=42,
        repository_full_name="acme/demo",
        payload={},
    )
    dispatcher = AsyncMock()
    dispatcher.mode = DispatchMode.OFF
    dispatcher.dispatch = AsyncMock(
        return_value=DispatchResult(
            mode=DispatchMode.OFF,
            event="pull_request",
            delivery_id="d-1",
            agents=(),
            outcome="off",
            duration_seconds=0.0,
        )
    )

    await _handle_webhook(parsed=parsed, dispatcher=dispatcher)

    from caretaker.fleet.store import get_store

    client = await (get_store()).get_client("acme/demo")
    assert client is None
