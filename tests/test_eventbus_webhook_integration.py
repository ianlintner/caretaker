"""End-to-end test: webhook handler publishes to the event bus, consumer dispatches.

We can't inspect the bus's internal queue post-publish because the
consumer task started in the FastAPI lifespan immediately drains it.
Instead we install a spy bus that captures every ``publish`` call. That
gives us the assertion surface (was it called, with what payload?) without
racing the consumer.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

import caretaker.mcp_backend.main as backend_main
from caretaker.eventbus.base import EventBusError
from caretaker.eventbus.local import LocalEventBus
from caretaker.state.dedup import LocalDedup

WEBHOOK_SECRET = "supersecret-test-value"


def _sign(secret: str, body: bytes) -> str:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _pr_payload() -> dict[str, Any]:
    return {
        "action": "opened",
        "installation": {"id": 42},
        "repository": {"full_name": "acme/demo"},
        "pull_request": {"number": 1},
    }


class _SpyBus(LocalEventBus):
    """LocalEventBus that records every publish call so tests can assert on them."""

    def __init__(self) -> None:
        super().__init__()
        self.published: list[tuple[str, dict[str, Any]]] = []

    async def publish(self, stream: str, payload: dict[str, Any]) -> str:  # type: ignore[override]
        event_id = await super().publish(stream, payload)
        self.published.append((stream, payload))
        return event_id


@pytest.fixture()
def spy_bus(monkeypatch: pytest.MonkeyPatch) -> _SpyBus:
    bus = _SpyBus()
    monkeypatch.setattr(backend_main, "_event_bus", bus)
    return bus


@pytest.fixture(autouse=True)
def _clean_dedup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(backend_main, "_dedup", LocalDedup())


@pytest.fixture(autouse=True)
def _disable_metrics_server(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevent metrics server from starting on port 9090 during tests."""
    monkeypatch.setenv("CARETAKER_METRICS_PORT", "0")


@pytest.fixture()
def with_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CARETAKER_GITHUB_APP_WEBHOOK_SECRET", WEBHOOK_SECRET)


def _post_webhook(client: TestClient, *, delivery_id: str) -> dict[str, Any]:
    body = json.dumps(_pr_payload()).encode()
    headers = {
        "X-GitHub-Event": "pull_request",
        "X-GitHub-Delivery": delivery_id,
        "X-Hub-Signature-256": _sign(WEBHOOK_SECRET, body),
        "Content-Type": "application/json",
    }
    resp = client.post("/webhooks/github", content=body, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_webhook_publishes_to_bus_when_dispatch_mode_not_off(
    with_secret: None, spy_bus: _SpyBus, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fresh delivery + non-off dispatch mode → exactly one publish."""
    monkeypatch.setenv("CARETAKER_WEBHOOK_DISPATCH_MODE", "shadow")
    monkeypatch.setattr(backend_main, "_dispatcher", None)

    with TestClient(backend_main.app, raise_server_exceptions=False) as client:
        body = _post_webhook(client, delivery_id="del-bus-1")
        assert body["dispatched"] is True

    publishes = [p for p in spy_bus.published if p[0] == "caretaker:events"]
    assert len(publishes) == 1
    payload = publishes[0][1]
    assert payload["delivery_id"] == "del-bus-1"
    assert payload["event_type"] == "pull_request"
    assert payload["repository_full_name"] == "acme/demo"


def test_webhook_does_not_publish_for_duplicate(
    with_secret: None, spy_bus: _SpyBus, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CARETAKER_WEBHOOK_DISPATCH_MODE", "shadow")
    monkeypatch.setattr(backend_main, "_dispatcher", None)

    with TestClient(backend_main.app, raise_server_exceptions=False) as client:
        first = _post_webhook(client, delivery_id="del-dup-1")
        second = _post_webhook(client, delivery_id="del-dup-1")

    assert first["duplicate"] is False
    assert second["duplicate"] is True

    publishes = [p for p in spy_bus.published if p[0] == "caretaker:events"]
    assert len(publishes) == 1, "duplicate delivery should not result in a second publish"


def test_webhook_falls_back_to_in_process_when_publish_raises(
    with_secret: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Redis outage → in-process fallback, still 200 to GitHub."""
    monkeypatch.setenv("CARETAKER_WEBHOOK_DISPATCH_MODE", "shadow")
    monkeypatch.setattr(backend_main, "_dispatcher", None)

    class _BrokenBus(LocalEventBus):
        async def publish(self, stream: str, payload: dict[str, Any]) -> str:  # type: ignore[override]
            raise EventBusError("simulated Redis outage")

    monkeypatch.setattr(backend_main, "_event_bus", _BrokenBus())

    with TestClient(backend_main.app, raise_server_exceptions=False) as client:
        body = _post_webhook(client, delivery_id="del-fallback-1")
        assert body["status"] == "accepted"
        assert body["duplicate"] is False
