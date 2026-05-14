"""Tests for the pre-escalation rung added in Task 4 of the openclaw integration.

Covers:
- should_attempt_pre_escalation predicate
- dispatch_pre_escalation_attempt happy path (returns True)
- dispatch_pre_escalation_attempt error path (returns False)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from caretaker.pr_reviewer import auto_fix as _auto_fix

# ── helpers ───────────────────────────────────────────────────────────────────


def _make_auto_fix_cfg(**kwargs):
    from caretaker.config import AutoFixConfig

    return AutoFixConfig(enabled=True, **kwargs)


def _make_pr_reviewer_cfg(**kwargs):
    from caretaker.config import PRReviewerConfig

    return PRReviewerConfig(enabled=True, **kwargs)


def _make_fake_pr(
    html_url: str = "https://github.com/owner/repo/pull/42", head_ref: str = "fix/my-branch"
):
    pr = MagicMock()
    pr.html_url = html_url
    pr.head_ref = head_ref
    return pr


# ── should_attempt_pre_escalation ─────────────────────────────────────────────


def test_should_attempt_pre_escalation_true():
    """Returns True when pre_escalation_agent is set and reason contains 'max_attempts'."""
    cfg = _make_auto_fix_cfg(pre_escalation_agent="openclaw_http")
    assert _auto_fix.should_attempt_pre_escalation("max_attempts=3 reached", cfg) is True


def test_should_attempt_pre_escalation_false_no_agent():
    """Returns False when pre_escalation_agent is empty, regardless of reason."""
    cfg = _make_auto_fix_cfg(pre_escalation_agent="")
    assert _auto_fix.should_attempt_pre_escalation("max_attempts=3 reached", cfg) is False


def test_should_attempt_pre_escalation_false_wrong_reason():
    """Returns False when reason does not contain 'max_attempts'."""
    cfg = _make_auto_fix_cfg(pre_escalation_agent="openclaw_http")
    assert _auto_fix.should_attempt_pre_escalation("pr_not_owned", cfg) is False


def test_should_attempt_pre_escalation_false_both_missing():
    """Returns False when both agent is empty and reason is unrelated."""
    cfg = _make_auto_fix_cfg(pre_escalation_agent="")
    assert _auto_fix.should_attempt_pre_escalation("some_other_reason", cfg) is False


# ── dispatch_pre_escalation_attempt ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_dispatch_pre_escalation_returns_true_on_success(monkeypatch):
    """When fix_run succeeds (no exception), dispatch_pre_escalation_attempt returns True."""
    fake_module = MagicMock()
    fake_module._prepare_workdir = AsyncMock(return_value=("/tmp/fake_workdir", MagicMock()))
    fake_module.fix_run = AsyncMock(return_value="Fixed successfully")

    monkeypatch.setattr(
        _auto_fix,
        "_resolve_backend_module",
        lambda backend: fake_module,
    )

    cfg = _make_auto_fix_cfg(pre_escalation_agent="openclaw_http")
    pr_reviewer_cfg = _make_pr_reviewer_cfg()
    pr = _make_fake_pr()

    result = await _auto_fix.dispatch_pre_escalation_attempt(
        pr,
        cfg,
        pr_reviewer_cfg,
        all_prior_errors="error from attempt 1\nerror from attempt 2",
        attempt_count=2,
    )

    assert result is True
    fake_module.fix_run.assert_awaited_once()
    call_kwargs = fake_module.fix_run.call_args.kwargs
    assert call_kwargs["workdir"] == "/tmp/fake_workdir"
    assert call_kwargs["prior_errors"] == "error from attempt 1\nerror from attempt 2"
    assert call_kwargs["attempt_count"] == 2


@pytest.mark.asyncio
async def test_dispatch_pre_escalation_returns_false_on_exception(monkeypatch):
    """When fix_run raises OpenclawHttpError, dispatch_pre_escalation_attempt returns False."""
    from caretaker.pr_reviewer.backends.openclaw_http import OpenclawHttpError

    fake_module = MagicMock()
    fake_module._prepare_workdir = AsyncMock(return_value=("/tmp/fake_workdir", MagicMock()))
    fake_module.fix_run = AsyncMock(side_effect=OpenclawHttpError("openclaw timeout"))

    monkeypatch.setattr(
        _auto_fix,
        "_resolve_backend_module",
        lambda backend: fake_module,
    )

    cfg = _make_auto_fix_cfg(pre_escalation_agent="openclaw_http")
    pr_reviewer_cfg = _make_pr_reviewer_cfg()
    pr = _make_fake_pr()

    result = await _auto_fix.dispatch_pre_escalation_attempt(
        pr,
        cfg,
        pr_reviewer_cfg,
        all_prior_errors="previous errors",
        attempt_count=3,
    )

    assert result is False


@pytest.mark.asyncio
async def test_dispatch_pre_escalation_returns_false_on_generic_exception(monkeypatch):
    """When fix_run raises any exception, dispatch_pre_escalation_attempt returns False."""
    fake_module = MagicMock()
    fake_module._prepare_workdir = AsyncMock(return_value=("/tmp/fake_workdir", MagicMock()))
    fake_module.fix_run = AsyncMock(side_effect=RuntimeError("unexpected failure"))

    monkeypatch.setattr(
        _auto_fix,
        "_resolve_backend_module",
        lambda backend: fake_module,
    )

    cfg = _make_auto_fix_cfg(pre_escalation_agent="openclaw_http")
    pr_reviewer_cfg = _make_pr_reviewer_cfg()
    pr = _make_fake_pr()

    result = await _auto_fix.dispatch_pre_escalation_attempt(
        pr,
        cfg,
        pr_reviewer_cfg,
        all_prior_errors="",
        attempt_count=1,
    )

    assert result is False
