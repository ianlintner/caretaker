"""FastAPI integration tests for the GitHub App routes in mcp_backend.main.

Covers:
  - POST /webhooks/github — signature verify, dedup, agent routing, error paths
  - GET  /oauth/callback  — env-gating and query-param validation
  - GET  /health          — sanity check
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from caretaker.mcp_backend.main import app
from caretaker.state.dedup import LocalDedup

# ── helpers ---------------------------------------------------------------


def _sign(secret: str, body: bytes) -> str:
    """Produce a valid ``X-Hub-Signature-256`` header value."""
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _webhook_headers(
    *,
    secret: str,
    body: bytes,
    event: str = "pull_request",
    delivery_id: str = "test-delivery-001",
    bad_sig: bool = False,
    omit_sig: bool = False,
) -> dict[str, str]:
    sig = _sign(secret, body)
    headers: dict[str, str] = {
        "X-GitHub-Event": event,
        "X-GitHub-Delivery": delivery_id,
        "Content-Type": "application/json",
    }
    if not omit_sig:
        headers["X-Hub-Signature-256"] = "sha256=deadbeef" if bad_sig else sig
    return headers


def _pr_payload(action: str = "opened", installation_id: int = 42) -> dict[str, Any]:
    return {
        "action": action,
        "installation": {"id": installation_id},
        "repository": {"full_name": "acme/demo"},
        "pull_request": {"number": 1},
    }


# ── fixtures ---------------------------------------------------------------


WEBHOOK_SECRET = "supersecret-test-value"

# Use a module-level TestClient so lifespan is shared; env patching is done
# per-test via monkeypatch.
client = TestClient(app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _clear_dedup(monkeypatch: pytest.MonkeyPatch):
    """Force an in-process LocalDedup for every test so tests are hermetic.

    Patches the module-level ``_dedup`` to a fresh ``LocalDedup`` regardless
    of what ``REDIS_URL`` may be set to in the test environment, ensuring tests
    never talk to a real Redis instance and always start with a clean state.
    """
    local = LocalDedup()
    monkeypatch.setattr("caretaker.mcp_backend.main._dedup", local)
    yield
    local._seen.clear()
    local._seen_set.clear()


@pytest.fixture()
def with_webhook_secret(monkeypatch: pytest.MonkeyPatch):
    """Set the webhook secret env var for the duration of the test."""
    monkeypatch.setenv("CARETAKER_GITHUB_APP_WEBHOOK_SECRET", WEBHOOK_SECRET)


@pytest.fixture()
def with_client_id(monkeypatch: pytest.MonkeyPatch):
    """Set the OAuth client-id env var for the duration of the test."""
    monkeypatch.setenv("CARETAKER_GITHUB_APP_CLIENT_ID", "Iv1.abc123")


# ── /health ---------------------------------------------------------------


def test_health_returns_200():
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert "version" in body


# ── /webhooks/github -------------------------------------------------------


def test_webhook_returns_503_when_secret_not_configured(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("CARETAKER_GITHUB_APP_WEBHOOK_SECRET_ENV", raising=False)
    monkeypatch.delenv("CARETAKER_GITHUB_APP_WEBHOOK_SECRET", raising=False)
    payload = json.dumps(_pr_payload()).encode()
    resp = client.post(
        "/webhooks/github",
        content=payload,
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 503


def test_webhook_rejects_missing_signature(with_webhook_secret):
    payload = json.dumps(_pr_payload()).encode()
    headers = _webhook_headers(
        secret=WEBHOOK_SECRET,
        body=payload,
        omit_sig=True,
    )
    resp = client.post("/webhooks/github", content=payload, headers=headers)
    assert resp.status_code == 401


def test_webhook_rejects_bad_signature(with_webhook_secret):
    payload = json.dumps(_pr_payload()).encode()
    headers = _webhook_headers(
        secret=WEBHOOK_SECRET,
        body=payload,
        bad_sig=True,
    )
    resp = client.post("/webhooks/github", content=payload, headers=headers)
    assert resp.status_code == 401


def test_webhook_accepts_valid_pull_request_event(with_webhook_secret):
    payload = json.dumps(_pr_payload()).encode()
    headers = _webhook_headers(
        secret=WEBHOOK_SECRET,
        body=payload,
        event="pull_request",
        delivery_id="delivery-pr-001",
    )
    resp = client.post("/webhooks/github", content=payload, headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "accepted"
    assert body["event"] == "pull_request"
    assert body["delivery_id"] == "delivery-pr-001"
    assert body["duplicate"] is False
    assert "pr" in body["agents"]
    assert body["installation_id"] == 42


def test_webhook_deduplicates_retried_delivery(with_webhook_secret):
    payload = json.dumps(_pr_payload()).encode()
    headers = _webhook_headers(
        secret=WEBHOOK_SECRET,
        body=payload,
        event="pull_request",
        delivery_id="delivery-dup-001",
    )
    resp1 = client.post("/webhooks/github", content=payload, headers=headers)
    assert resp1.status_code == 200
    assert resp1.json()["duplicate"] is False

    resp2 = client.post("/webhooks/github", content=payload, headers=headers)
    assert resp2.status_code == 200
    assert resp2.json()["duplicate"] is True


def test_webhook_empty_agents_for_ping(with_webhook_secret):
    payload = json.dumps({"zen": "keep it simple", "hook_id": 1}).encode()
    headers = _webhook_headers(
        secret=WEBHOOK_SECRET,
        body=payload,
        event="ping",
        delivery_id="ping-delivery-001",
    )
    resp = client.post("/webhooks/github", content=payload, headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["event"] == "ping"
    assert body["agents"] == []


def test_webhook_rejects_malformed_json(with_webhook_secret):
    payload = b"not-valid-json"
    headers = _webhook_headers(
        secret=WEBHOOK_SECRET,
        body=payload,
        event="push",
        delivery_id="delivery-bad-json-001",
    )
    resp = client.post("/webhooks/github", content=payload, headers=headers)
    assert resp.status_code == 400


def test_webhook_security_event_routed(with_webhook_secret):
    payload = json.dumps(
        {
            "action": "created",
            "alert": {"number": 7},
            "installation": {"id": 99},
            "repository": {"full_name": "acme/demo"},
        }
    ).encode()
    headers = _webhook_headers(
        secret=WEBHOOK_SECRET,
        body=payload,
        event="dependabot_alert",
        delivery_id="delivery-sec-001",
    )
    resp = client.post("/webhooks/github", content=payload, headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert "security" in body["agents"]


def test_webhook_no_installation_id_when_absent(with_webhook_secret):
    """Payload without an installation key → installation_id is None."""
    payload = json.dumps(
        {
            "action": "opened",
            "repository": {"full_name": "acme/demo"},
            "pull_request": {"number": 2},
        }
    ).encode()
    headers = _webhook_headers(
        secret=WEBHOOK_SECRET,
        body=payload,
        event="pull_request",
        delivery_id="delivery-no-install-001",
    )
    resp = client.post("/webhooks/github", content=payload, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["installation_id"] is None


# ── /oauth/callback -------------------------------------------------------


def test_oauth_callback_503_when_client_id_absent(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("CARETAKER_GITHUB_APP_CLIENT_ID_ENV", raising=False)
    monkeypatch.delenv("CARETAKER_GITHUB_APP_CLIENT_ID", raising=False)
    resp = client.get("/oauth/callback", params={"code": "somecode"})
    assert resp.status_code == 503


def test_oauth_callback_400_when_code_missing(with_client_id):
    resp = client.get("/oauth/callback")
    assert resp.status_code == 400
    assert "code" in resp.json()["detail"]


def test_oauth_callback_200_when_configured(with_client_id):
    resp = client.get("/oauth/callback", params={"code": "abc123", "state": "xyz"})
    assert resp.status_code == 200
    assert b"caretaker" in resp.content
