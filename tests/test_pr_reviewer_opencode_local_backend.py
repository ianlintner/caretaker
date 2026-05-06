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
    _resolve_tier_model,
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


def test_parse_review_payload_fallback_returns_was_fallback_flag(caplog) -> None:
    """When the ``caretaker-review`` JSON block is missing, the fallback flag is True.

    The metric is now recorded by the caller (``run`` / ``fix_run``)
    with the real model + mode labels — see ``run()`` for the
    matching ``record_opencode_invocation`` call. ``_parse_review_payload``
    only signals "I took the fallback branch" via the bool.
    """
    with caplog.at_level("WARNING"):
        result, was_fallback = _parse_review_payload("Just prose, no JSON. Looks fine.")

    assert result.verdict == "COMMENT"
    assert was_fallback is True
    # The warning text mentions "fallback parse" so operators can grep.
    assert any("fallback parse" in rec.message for rec in caplog.records)


def test_parse_review_payload_happy_path_returns_false_fallback_flag() -> None:
    """A well-formed reply yields ``was_fallback=False``."""
    text = (
        "Some prose summary.\n\n"
        "<!-- caretaker:review-result -->\n"
        "```caretaker-review\n"
        '{"verdict": "APPROVE", "summary": "lgtm", "comments": []}\n'
        "```\n"
    )
    result, was_fallback = _parse_review_payload(text)
    assert result.verdict == "APPROVE"
    assert was_fallback is False


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


# ── _resolve_tier_model ──────────────────────────────────────────────────


def test_resolve_tier_model_uses_tier_map_entry() -> None:
    tier_map = {"trivial": "cheap-model", "standard": "expensive-model"}
    assert _resolve_tier_model("trivial", tier_map=tier_map, default="default") == "cheap-model"
    assert (
        _resolve_tier_model("standard", tier_map=tier_map, default="default") == "expensive-model"
    )


def test_resolve_tier_model_falls_back_when_tier_missing() -> None:
    tier_map = {"trivial": "cheap-model"}
    assert _resolve_tier_model("complex", tier_map=tier_map, default="default") == "default"


def test_resolve_tier_model_falls_back_when_tier_is_none() -> None:
    tier_map = {"trivial": "cheap-model"}
    assert _resolve_tier_model(None, tier_map=tier_map, default="default") == "default"


def test_resolve_tier_model_falls_back_when_entry_is_empty() -> None:
    tier_map = {"trivial": ""}
    assert _resolve_tier_model("trivial", tier_map=tier_map, default="default") == "default"


# ── _invoke_opencode "No endpoints found" detection ──────────────────────


@pytest.mark.asyncio
async def test_invoke_opencode_raises_no_endpoints_on_stderr_match(
    monkeypatch, fake_config
) -> None:
    """A non-zero return code + ``No endpoints found`` stderr → ``OpenCodeLocalNoEndpointsError``.

    Exercises the actual stderr-pattern detection (rather than mocking
    the exception itself) so a future refactor that drops the
    string-match doesn't slip through with green tests.
    """
    fake_proc = MagicMock()
    fake_proc.returncode = 1

    async def _fake_create_subprocess_exec(*_args, **_kwargs):
        return fake_proc

    monkeypatch.setattr(
        opencode_local.asyncio, "create_subprocess_exec", _fake_create_subprocess_exec
    )
    monkeypatch.setattr(
        opencode_local,
        "stream_subprocess_output",
        AsyncMock(
            return_value=(
                "",
                "Error: No endpoints found for model openrouter/foo/bar",
            )
        ),
    )
    # Bypass the ``shutil.which`` lookup so the binary always "exists".
    monkeypatch.setattr(opencode_local.shutil, "which", lambda _name: "/usr/bin/opencode")

    with pytest.raises(OpenCodeLocalNoEndpointsError) as excinfo:
        await opencode_local._invoke_opencode(
            workdir="/tmp/fake", config=fake_config, prompt="hi", model_override=""
        )
    assert "No endpoints found" in str(excinfo.value)
