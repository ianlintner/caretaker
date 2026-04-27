"""Tests for the HMAC-signed ingest token."""

from __future__ import annotations

import time

import pytest

from caretaker.runs import tokens
from caretaker.runs.tokens import IngestPurpose, IngestTokenError


@pytest.fixture(autouse=True)
def _set_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "CARETAKER_RUNS_INGEST_TOKEN_SECRET",
        "test-secret-do-not-use-in-prod-32-bytes-min",
    )


def test_issue_and_verify_round_trip() -> None:
    token = tokens.issue(run_id="abc", purpose=IngestPurpose.LOGS, ttl_seconds=60)
    principal = tokens.verify(token, run_id="abc")
    assert principal.run_id == "abc"
    assert principal.purpose is IngestPurpose.LOGS


def test_purpose_enforcement() -> None:
    token = tokens.issue(run_id="abc", purpose=IngestPurpose.LOGS)
    with pytest.raises(IngestTokenError, match="purpose"):
        tokens.verify(token, run_id="abc", require_purpose=IngestPurpose.FINISH)


def test_purpose_any_passes_specific_check() -> None:
    token = tokens.issue(run_id="abc", purpose=IngestPurpose.ANY)
    assert (
        tokens.verify(token, run_id="abc", require_purpose=IngestPurpose.FINISH).purpose
        is IngestPurpose.ANY
    )


def test_run_id_binding() -> None:
    token = tokens.issue(run_id="abc")
    with pytest.raises(IngestTokenError, match="bound to a different run"):
        tokens.verify(token, run_id="xyz")


def test_expired_token_rejected() -> None:
    token = tokens.issue(run_id="abc", ttl_seconds=10, now=int(time.time()) - 100)
    with pytest.raises(IngestTokenError, match="expired"):
        tokens.verify(token, run_id="abc")


def test_signature_tamper_rejected() -> None:
    token = tokens.issue(run_id="abc")
    parts = token.split(".")
    parts[-1] = "AAAA" + parts[-1][4:]  # corrupt signature
    bad = ".".join(parts)
    with pytest.raises(IngestTokenError, match="signature mismatch"):
        tokens.verify(bad, run_id="abc")


def test_payload_tamper_rejected() -> None:
    token = tokens.issue(run_id="abc")
    parts = token.split(".")
    parts[1] = "xyz"  # change run_id in payload (signature no longer matches)
    bad = ".".join(parts)
    with pytest.raises(IngestTokenError):
        tokens.verify(bad, run_id="abc")


def test_missing_secret_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CARETAKER_RUNS_INGEST_TOKEN_SECRET", raising=False)
    with pytest.raises(IngestTokenError, match="not configured"):
        tokens.issue(run_id="abc")
