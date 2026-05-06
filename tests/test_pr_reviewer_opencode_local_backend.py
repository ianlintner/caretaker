"""Tests for the opencode_local backend.

Like the ``claude_code_local`` test file, the heavy paths (clone +
opencode-CLI invocation) are out of scope for unit tests. This file
focuses on the pure-Python parts plus the metric wire-up:

  * fallback warning + ``parse_fallback`` counter from
    :func:`_parse_review_payload`
  * ``ok`` outcome on the happy path of :func:`run`
  * ``no_endpoints`` outcome when stderr matches the opencode "No
    endpoints found" sentinel
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from caretaker.pr_reviewer.backends import opencode_local
from caretaker.pr_reviewer.backends.opencode_local import (
    OpenCodeLocalNoEndpointsError,
    _parse_review_payload,
)


def _read_invoke_counter(model: str, mode: str, outcome: str) -> float:
    from caretaker.observability.metrics import REGISTRY, get_service_label

    val = REGISTRY.get_sample_value(
        "caretaker_opencode_invocation_total",
        {
            "service": get_service_label(),
            "model": model,
            "mode": mode,
            "outcome": outcome,
        },
    )
    return 0.0 if val is None else float(val)


# ── _parse_review_payload fallback warning + counter ─────────────────────


def test_parse_review_payload_fallback_records_parse_fallback_counter(caplog) -> None:
    """When the ``caretaker-review`` JSON block is missing, log + counter fire."""
    before = _read_invoke_counter("<unknown>", "other", "parse_fallback")

    with caplog.at_level("WARNING"):
        result = _parse_review_payload("Just prose, no JSON. Looks fine.")

    assert result.verdict == "COMMENT"
    after = _read_invoke_counter("<unknown>", "other", "parse_fallback")
    assert after >= before + 1
    # The warning text mentions "fallback parse" so operators can grep.
    assert any("fallback parse" in rec.message for rec in caplog.records)


# ── run() outcome wiring ──────────────────────────────────────────────────


def _structured_payload() -> str:
    return (
        "Some prose summary.\n\n"
        "<!-- caretaker:review-result -->\n"
        "```caretaker-review\n"
        '{"verdict": "APPROVE", "summary": "lgtm", "comments": []}\n'
        "```\n"
    )


@pytest.fixture
def fake_config() -> MagicMock:
    cfg = MagicMock()
    cfg.review_models = {}
    cfg.fix_models = {}
    cfg.model = "openrouter/test/model"
    cfg.fix_model = ""
    cfg.clone_depth = 1
    cfg.clone_workdir_root = ""
    cfg.timeout_seconds = 60
    cfg.cli_path = "opencode"
    cfg.extra_env = {}
    cfg.keep_workdir_on_failure = False
    return cfg


@pytest.mark.asyncio
async def test_run_records_ok_outcome_on_happy_path(monkeypatch, fake_config) -> None:
    """Structured JSON parse → outcome=ok with the resolved review model."""
    monkeypatch.setattr(
        opencode_local,
        "_prepare_workdir",
        AsyncMock(return_value=("/tmp/fake", MagicMock())),
    )
    monkeypatch.setattr(opencode_local, "cleanup_workdir", MagicMock())
    monkeypatch.setattr(
        opencode_local, "_invoke_opencode", AsyncMock(return_value=_structured_payload())
    )

    before = _read_invoke_counter("openrouter/test/model", "review", "ok")
    result = await opencode_local.run(pr_url="https://github.com/o/r/pull/1", config=fake_config)
    after = _read_invoke_counter("openrouter/test/model", "review", "ok")
    assert result.verdict == "APPROVE"
    assert after >= before + 1


@pytest.mark.asyncio
async def test_run_records_parse_fallback_outcome(monkeypatch, fake_config) -> None:
    """Missing structured JSON → outcome=parse_fallback (model-aware label)."""
    monkeypatch.setattr(
        opencode_local,
        "_prepare_workdir",
        AsyncMock(return_value=("/tmp/fake", MagicMock())),
    )
    monkeypatch.setattr(opencode_local, "cleanup_workdir", MagicMock())
    # No caretaker-review fence — _parse_review_payload takes the fallback.
    monkeypatch.setattr(
        opencode_local, "_invoke_opencode", AsyncMock(return_value="Just prose, no JSON.")
    )

    before = _read_invoke_counter("openrouter/test/model", "review", "parse_fallback")
    result = await opencode_local.run(pr_url="https://github.com/o/r/pull/2", config=fake_config)
    after = _read_invoke_counter("openrouter/test/model", "review", "parse_fallback")
    assert result.verdict == "COMMENT"
    assert after >= before + 1


@pytest.mark.asyncio
async def test_run_records_no_endpoints_outcome(monkeypatch, fake_config) -> None:
    """``OpenCodeLocalNoEndpointsError`` → outcome=no_endpoints, exception re-raised."""
    monkeypatch.setattr(
        opencode_local,
        "_prepare_workdir",
        AsyncMock(return_value=("/tmp/fake", MagicMock())),
    )
    monkeypatch.setattr(opencode_local, "cleanup_workdir", MagicMock())
    monkeypatch.setattr(
        opencode_local,
        "_invoke_opencode",
        AsyncMock(side_effect=OpenCodeLocalNoEndpointsError("No endpoints found")),
    )

    before = _read_invoke_counter("openrouter/test/model", "review", "no_endpoints")
    with pytest.raises(OpenCodeLocalNoEndpointsError):
        await opencode_local.run(pr_url="https://github.com/o/r/pull/3", config=fake_config)
    after = _read_invoke_counter("openrouter/test/model", "review", "no_endpoints")
    assert after >= before + 1
