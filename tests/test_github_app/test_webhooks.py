"""Tests for ``verify_signature`` and ``parse_webhook``."""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from caretaker.github_app.webhooks import (
    WebhookSignatureError,
    parse_webhook,
    verify_signature,
)


def _sign(secret: str, body: bytes) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def test_verify_signature_happy_path() -> None:
    secret = "s3cret"
    body = b'{"action":"opened"}'
    verify_signature(secret=secret, body=body, signature_header=_sign(secret, body))


def test_verify_signature_rejects_mismatch() -> None:
    secret = "s3cret"
    body = b'{"action":"opened"}'
    with pytest.raises(WebhookSignatureError, match="signature mismatch"):
        verify_signature(
            secret=secret,
            body=body,
            signature_header=_sign("other", body),
        )


def test_verify_signature_rejects_tampered_body() -> None:
    secret = "s3cret"
    body = b'{"action":"opened"}'
    header = _sign(secret, body)
    with pytest.raises(WebhookSignatureError, match="signature mismatch"):
        verify_signature(
            secret=secret,
            body=body + b"x",
            signature_header=header,
        )


def test_verify_signature_rejects_missing_header() -> None:
    with pytest.raises(WebhookSignatureError, match="missing"):
        verify_signature(secret="s", body=b"{}", signature_header=None)


def test_verify_signature_rejects_unknown_scheme() -> None:
    with pytest.raises(WebhookSignatureError, match="unexpected signature scheme"):
        verify_signature(secret="s", body=b"{}", signature_header="sha1=abc")


def test_verify_signature_rejects_empty_secret() -> None:
    with pytest.raises(WebhookSignatureError, match="not configured"):
        verify_signature(secret="", body=b"{}", signature_header="sha256=abc")


def test_parse_webhook_extracts_relevant_fields() -> None:
    payload = {
        "action": "opened",
        "installation": {"id": 999},
        "repository": {"full_name": "acme/widgets"},
        "pull_request": {"number": 7},
    }
    body = json.dumps(payload).encode("utf-8")
    parsed = parse_webhook(
        body=body,
        headers={
            "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": "abc-123",
        },
    )
    assert parsed.event_type == "pull_request"
    assert parsed.delivery_id == "abc-123"
    assert parsed.action == "opened"
    assert parsed.installation_id == 999
    assert parsed.repository_full_name == "acme/widgets"
    assert parsed.payload["pull_request"]["number"] == 7


def test_parse_webhook_tolerates_missing_optional_fields() -> None:
    body = b"{}"
    parsed = parse_webhook(
        body=body,
        headers={
            "x-github-event": "ping",
            "x-github-delivery": "ping-1",
        },
    )
    assert parsed.event_type == "ping"
    assert parsed.action is None
    assert parsed.installation_id is None
    assert parsed.repository_full_name is None


def test_parse_webhook_rejects_missing_event_header() -> None:
    with pytest.raises(ValueError, match="X-GitHub-Event"):
        parse_webhook(body=b"{}", headers={"X-GitHub-Delivery": "abc"})


def test_parse_webhook_rejects_missing_delivery_header() -> None:
    with pytest.raises(ValueError, match="X-GitHub-Delivery"):
        parse_webhook(body=b"{}", headers={"X-GitHub-Event": "ping"})


def test_parse_webhook_rejects_invalid_json() -> None:
    with pytest.raises(ValueError, match="valid JSON"):
        parse_webhook(
            body=b"not json",
            headers={"X-GitHub-Event": "ping", "X-GitHub-Delivery": "d"},
        )


def test_parse_webhook_rejects_non_object_root() -> None:
    with pytest.raises(ValueError, match="must be an object"):
        parse_webhook(
            body=b"[]",
            headers={"X-GitHub-Event": "ping", "X-GitHub-Delivery": "d"},
        )


def test_parse_webhook_ignores_bogus_installation_id() -> None:
    body = json.dumps({"installation": {"id": "not-an-int"}}).encode("utf-8")
    parsed = parse_webhook(
        body=body,
        headers={"X-GitHub-Event": "ping", "X-GitHub-Delivery": "d"},
    )
    assert parsed.installation_id is None
