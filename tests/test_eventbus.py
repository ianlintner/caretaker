"""Tests for the LocalEventBus and webhook consumer integration.

Redis-backed tests live under tests/integration/ when a real Redis is
available — keeping these fast and dependency-free covers the contract
the dispatcher consumer relies on (publish, ordered consume, ack-on-success,
keep-on-raise, claim-idle redelivery).
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from caretaker.eventbus import (
    LocalEventBus,
    build_event_bus,
    reset_event_bus,
    webhook_event_payload,
)
from caretaker.eventbus.local import LocalEventBus as _LocalEventBus
from caretaker.github_app.webhooks import ParsedWebhook


@pytest.fixture(autouse=True)
def _clear_event_bus_singleton() -> None:
    """Drop the module-level event-bus singleton between tests."""
    reset_event_bus()
    yield
    reset_event_bus()


@pytest.mark.asyncio
async def test_publish_then_consume_delivers_one_event() -> None:
    bus = LocalEventBus()
    received: list[dict] = []

    async def handler(event):  # type: ignore[no-untyped-def]
        received.append(event.payload)

    await bus.publish("s", {"hello": "world"})

    task = asyncio.create_task(
        bus.consume(
            stream="s",
            group="g",
            consumer="c",
            handler=handler,
            block_ms=200,
            batch_size=10,
        )
    )
    await asyncio.sleep(0.3)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert received == [{"hello": "world"}]


@pytest.mark.asyncio
async def test_handler_raise_keeps_message_in_pel() -> None:
    bus = LocalEventBus()
    seen: list[str] = []

    async def handler(event):  # type: ignore[no-untyped-def]
        seen.append(event.id)
        raise RuntimeError("boom")

    await bus.publish("s", {"k": 1})
    task = asyncio.create_task(
        bus.consume(stream="s", group="g", consumer="c", handler=handler, block_ms=200)
    )
    await asyncio.sleep(0.3)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    # Handler ran once, raised, message should still be in the PEL.
    assert len(seen) == 1
    state = bus._streams["s"]  # type: ignore[attr-defined]
    assert len(state.groups["g"]) == 1


@pytest.mark.asyncio
async def test_claim_idle_redelivers_after_threshold() -> None:
    bus = LocalEventBus()
    attempts: list[str] = []

    async def flaky_handler(event):  # type: ignore[no-untyped-def]
        attempts.append(event.id)
        if len(attempts) == 1:
            raise RuntimeError("first attempt fails")

    await bus.publish("s", {"k": 1})
    task = asyncio.create_task(
        bus.consume(stream="s", group="g", consumer="c", handler=flaky_handler, block_ms=200)
    )
    await asyncio.sleep(0.3)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    # 0ms idle threshold — every PEL entry is fair game immediately.
    handled = await bus.claim_idle(
        stream="s", group="g", consumer="c", min_idle_ms=0, handler=flaky_handler
    )
    assert handled == 1
    assert len(attempts) == 2  # original failed delivery + one redelivery


@pytest.mark.asyncio
async def test_two_consumers_share_load() -> None:
    bus = LocalEventBus()
    a_received: list[str] = []
    b_received: list[str] = []

    async def handler_a(event):  # type: ignore[no-untyped-def]
        a_received.append(event.id)

    async def handler_b(event):  # type: ignore[no-untyped-def]
        b_received.append(event.id)

    for i in range(20):
        await bus.publish("s", {"i": i})

    t_a = asyncio.create_task(
        bus.consume(stream="s", group="g", consumer="a", handler=handler_a, block_ms=200)
    )
    t_b = asyncio.create_task(
        bus.consume(stream="s", group="g", consumer="b", handler=handler_b, block_ms=200)
    )
    await asyncio.sleep(0.4)
    for t in (t_a, t_b):
        t.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await t

    # Each event must be delivered to exactly one consumer.
    assert len(a_received) + len(b_received) == 20
    assert set(a_received).isdisjoint(set(b_received))


def test_factory_returns_local_when_no_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REDIS_URL", raising=False)
    bus = build_event_bus()
    assert isinstance(bus, _LocalEventBus)


def test_webhook_event_payload_roundtrip() -> None:
    parsed = ParsedWebhook(
        event_type="pull_request",
        delivery_id="abc-123",
        action="opened",
        installation_id=42,
        repository_full_name="owner/repo",
        payload={"pull_request": {"number": 7}},
    )
    payload = webhook_event_payload(parsed)
    assert payload["delivery_id"] == "abc-123"
    assert payload["event_type"] == "pull_request"
    assert payload["installation_id"] == 42
    assert payload["repository_full_name"] == "owner/repo"
    assert payload["raw_payload"] == {"pull_request": {"number": 7}}
