"""Tests for the auto-fix dispatcher (classify_issue_categories, decide_auto_fix,
dispatch_auto_fix).

Coverage:
- classify_issue_categories: reviewer-supplied vs heuristic categories
- decide_auto_fix: eligibility gates and backend routing
- parse_review_payload: issue_categories extraction/filtering
- dispatch_auto_fix: counter invariants, lint paths, tracking updates
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from caretaker.pr_reviewer import auto_fix as _auto_fix
from caretaker.pr_reviewer.inline_reviewer import ReviewResult

# ── helpers ──────────────────────────────────────────────────────────────────


def _make_tracking(**kwargs):
    from caretaker.state.models import TrackedPR

    return TrackedPR(number=1, **kwargs)


def _make_config(**kwargs):
    from caretaker.config import AutoFixConfig

    return AutoFixConfig(enabled=True, **kwargs)


# ── classify_issue_categories ─────────────────────────────────────────────────


def test_classify_uses_reviewer_categories_when_present():
    result = ReviewResult(
        summary="foo",
        verdict="REQUEST_CHANGES",
        comments=[],
        issue_categories=["lint", "format"],
    )
    assert _auto_fix.classify_issue_categories(result) == ["lint", "format"]


def test_classify_heuristic_matches_lint_keyword():
    result = ReviewResult(
        summary="Found ruff violations in file",
        verdict="REQUEST_CHANGES",
        comments=[],
    )
    cats = _auto_fix.classify_issue_categories(result)
    assert "lint" in cats


def test_classify_returns_empty_when_no_match():
    result = ReviewResult(
        summary="Looks good overall",
        verdict="REQUEST_CHANGES",
        comments=[],
    )
    assert _auto_fix.classify_issue_categories(result) == []


# ── decide_auto_fix ───────────────────────────────────────────────────────────


async def test_decide_disabled_returns_no_dispatch():
    from caretaker.config import AutoFixConfig

    cfg = AutoFixConfig(enabled=False)
    review = ReviewResult(summary="x", verdict="REQUEST_CHANGES", comments=[])
    d = await _auto_fix.decide_auto_fix(
        review=review,
        config=cfg,
        pr_author="bot[bot]",
        pr_labels=[],
        tracking=_make_tracking(),
    )
    assert not d.should_dispatch


async def test_decide_non_request_changes_returns_no_dispatch():
    cfg = _make_config()
    review = ReviewResult(summary="x", verdict="APPROVE", comments=[])
    d = await _auto_fix.decide_auto_fix(
        review=review,
        config=cfg,
        pr_author="bot[bot]",
        pr_labels=[],
        tracking=_make_tracking(),
    )
    assert not d.should_dispatch


async def test_decide_max_attempts_reached():
    cfg = _make_config(max_attempts=2)
    review = ReviewResult(summary="x", verdict="REQUEST_CHANGES", comments=[])
    tracking = _make_tracking(auto_fix_attempts=2)
    d = await _auto_fix.decide_auto_fix(
        review=review,
        config=cfg,
        pr_author="bot[bot]",
        pr_labels=[],
        tracking=tracking,
    )
    assert not d.should_dispatch
    assert "max_attempts" in d.reason


async def test_decide_author_eligible_dispatches():
    cfg = _make_config(allowed_authors=["copilot-swe-agent[bot]"])
    review = ReviewResult(
        summary="lint error",
        verdict="REQUEST_CHANGES",
        comments=[],
        issue_categories=["lint"],
    )
    d = await _auto_fix.decide_auto_fix(
        review=review,
        config=cfg,
        pr_author="copilot-swe-agent[bot]",
        pr_labels=[],
        tracking=_make_tracking(),
    )
    assert d.should_dispatch
    assert d.backend == "deterministic_lint"


async def test_decide_label_opt_in_eligible():
    cfg = _make_config(opt_in_label="caretaker:auto-fix")
    review = ReviewResult(
        summary="correctness bug",
        verdict="REQUEST_CHANGES",
        comments=[],
        issue_categories=["correctness"],
    )
    d = await _auto_fix.decide_auto_fix(
        review=review,
        config=cfg,
        pr_author="human-dev",
        pr_labels=["caretaker:auto-fix"],
        tracking=_make_tracking(),
    )
    assert d.should_dispatch


async def test_decide_unknown_author_no_label_denied():
    cfg = _make_config()
    review = ReviewResult(summary="x", verdict="REQUEST_CHANGES", comments=[])
    d = await _auto_fix.decide_auto_fix(
        review=review,
        config=cfg,
        pr_author="random-human",
        pr_labels=[],
        tracking=_make_tracking(),
    )
    assert not d.should_dispatch


async def test_decide_routes_lint_to_deterministic():
    cfg = _make_config(allowed_authors=["bot[bot]"])
    review = ReviewResult(
        summary="x",
        verdict="REQUEST_CHANGES",
        comments=[],
        issue_categories=["lint"],
    )
    d = await _auto_fix.decide_auto_fix(
        review=review,
        config=cfg,
        pr_author="bot[bot]",
        pr_labels=[],
        tracking=_make_tracking(),
    )
    assert d.backend == "deterministic_lint"


async def test_decide_falls_back_to_default_fixer():
    cfg = _make_config(allowed_authors=["bot[bot]"], default_fixer="claude_code_local")
    review = ReviewResult(
        summary="x",
        verdict="REQUEST_CHANGES",
        comments=[],
        issue_categories=[],
    )
    d = await _auto_fix.decide_auto_fix(
        review=review,
        config=cfg,
        pr_author="bot[bot]",
        pr_labels=[],
        tracking=_make_tracking(),
    )
    assert d.backend == "claude_code_local"


# ── parse_review_payload issue_categories ─────────────────────────────────────


def test_parse_review_payload_extracts_issue_categories():
    from caretaker.pr_reviewer.handoff_review_consumer import parse_review_payload

    body = (
        "\nSome preamble.\n\n"
        "```caretaker-review\n"
        '{"summary": "Found lint issues.", "verdict": "REQUEST_CHANGES",'
        ' "comments": [], "issue_categories": ["lint", "format"]}\n'
        "```\n"
    )
    result = parse_review_payload(body)
    assert result is not None
    assert result.issue_categories == ["lint", "format"]


def test_parse_review_payload_rejects_invalid_categories():
    from caretaker.pr_reviewer.handoff_review_consumer import parse_review_payload

    body = (
        "\n```caretaker-review\n"
        '{"summary": "Issues found.", "verdict": "REQUEST_CHANGES",'
        ' "comments": [], "issue_categories": ["lint", "INVALID_CATEGORY"]}\n'
        "```\n"
    )
    result = parse_review_payload(body)
    assert result is not None
    assert result.issue_categories == ["lint"]  # INVALID_CATEGORY filtered out


# ── always_run_heuristic merge logic ─────────────────────────────────────────


async def test_decide_always_run_heuristic_merges_categories():
    """always_run_heuristic=True should add heuristic matches not already in LLM categories."""
    cfg = _make_config(
        allowed_authors=["bot[bot]"],
        always_run_heuristic=True,
    )
    review = ReviewResult(
        summary="Found ruff lint errors",
        verdict="REQUEST_CHANGES",
        comments=[],
        issue_categories=["correctness"],  # LLM-supplied category
    )
    d = await _auto_fix.decide_auto_fix(
        review=review,
        config=cfg,
        pr_author="bot[bot]",
        pr_labels=[],
        tracking=_make_tracking(),
    )
    assert d.should_dispatch
    # "correctness" was LLM-supplied; heuristic should add "lint" from summary text
    assert "correctness" in (d.categories or [])
    assert "lint" in (d.categories or [])


async def test_decide_always_run_heuristic_no_duplicates():
    """always_run_heuristic=True must not duplicate categories already present."""
    cfg = _make_config(
        allowed_authors=["bot[bot]"],
        always_run_heuristic=True,
    )
    review = ReviewResult(
        summary="Found ruff lint errors",
        verdict="REQUEST_CHANGES",
        comments=[],
        issue_categories=["lint"],  # already present — heuristic would also match "lint"
    )
    d = await _auto_fix.decide_auto_fix(
        review=review,
        config=cfg,
        pr_author="bot[bot]",
        pr_labels=[],
        tracking=_make_tracking(),
    )
    assert (d.categories or []).count("lint") == 1


# ── _workdir.py token sanitization ───────────────────────────────────────────


def test_run_git_sanitizes_token_in_error(monkeypatch):
    """_run_git must not leak x-access-token credentials in WorkdirError messages."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    import caretaker.pr_reviewer.backends._workdir as _workdir_mod
    from caretaker.pr_reviewer.backends._workdir import WorkdirError

    # Fake a subprocess that exits with returncode=128 (auth failure).
    fake_proc = MagicMock()
    fake_proc.returncode = 128

    async def _fake_stream(proc, *, timeout_seconds, stdout_log, stderr_log):
        return ("", "authentication failed")

    monkeypatch.setattr(
        _workdir_mod,
        "stream_subprocess_output",
        _fake_stream,
    )
    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        AsyncMock(return_value=fake_proc),
    )

    async def _run():
        try:
            await _workdir_mod._run_git(
                "clone",
                "https://x-access-token:supersecrettoken@github.com/owner/repo.git",
                "/tmp/dest",
            )
        except WorkdirError as exc:
            return str(exc)
        return ""

    msg = asyncio.run(_run())
    assert msg, "expected WorkdirError but got empty string"
    assert "supersecrettoken" not in msg, f"token leaked in: {msg!r}"
    assert "x-access-token:***@" in msg, f"sanitized placeholder missing in: {msg!r}"


# ── claude_code_local _parse_review_payload issue_categories ─────────────────


def test_parse_review_payload_extracts_issue_categories_claude_code_local():
    """_parse_review_payload in claude_code_local should extract issue_categories."""
    from caretaker.pr_reviewer.backends.claude_code_local import _parse_review_payload

    assistant_text = (
        "Some prose.\n\n"
        "<!-- caretaker:review-result -->\n"
        "```caretaker-review\n"
        '{"verdict": "REQUEST_CHANGES", "summary": "Found lint issues.",'
        ' "comments": [], "issue_categories": ["lint", "format"]}\n'
        "```\n"
    )
    result = _parse_review_payload(assistant_text)
    assert result.verdict == "REQUEST_CHANGES"
    assert result.issue_categories == ["lint", "format"]


def test_parse_review_payload_filters_invalid_categories_claude_code_local():
    """_parse_review_payload should reject unknown category values."""
    from caretaker.pr_reviewer.backends.claude_code_local import _parse_review_payload

    assistant_text = (
        "```caretaker-review\n"
        '{"verdict": "REQUEST_CHANGES", "summary": "Issues.",'
        ' "comments": [], "issue_categories": ["security", "NOT_VALID"]}\n'
        "```\n"
    )
    result = _parse_review_payload(assistant_text)
    assert result.issue_categories == ["security"]


def test_parse_review_payload_empty_categories_when_absent():
    """_parse_review_payload should default to [] when issue_categories not in payload."""
    from caretaker.pr_reviewer.backends.claude_code_local import _parse_review_payload

    assistant_text = (
        "```caretaker-review\n"
        '{"verdict": "APPROVE", "summary": "Looks good.", "comments": []}\n'
        "```\n"
    )
    result = _parse_review_payload(assistant_text)
    assert result.issue_categories == []


# ── dispatch_auto_fix ─────────────────────────────────────────────────────────


def _make_dispatch_review(**kwargs) -> ReviewResult:
    return ReviewResult(
        summary="lint errors found", verdict="REQUEST_CHANGES", comments=[], **kwargs
    )


def _make_dispatch_decision(**kwargs) -> _auto_fix.AutoFixDecision:
    return _auto_fix.AutoFixDecision(
        should_dispatch=True,
        backend="deterministic_lint",
        reason="test",
        categories=["lint"],
        **kwargs,
    )


@pytest.mark.asyncio
async def test_dispatch_auto_fix_increments_attempt_counter_before_fixer_runs(monkeypatch):
    """auto_fix_attempts is incremented BEFORE the fixer runs.

    Critical invariant: a crash mid-fix (here: missing GITHUB_TOKEN) still
    consumes one attempt so the loop stays bounded.
    """
    from caretaker.config import PRReviewerConfig
    from caretaker.state.models import TrackedPR

    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    github = MagicMock()
    github.upsert_issue_comment = AsyncMock()
    tracking = TrackedPR(number=1)

    outcome = await _auto_fix.dispatch_auto_fix(
        decision=_make_dispatch_decision(),
        pr_url="https://github.com/owner/repo/pull/1",
        head_branch="feature/fix",
        review=_make_dispatch_review(),
        config=PRReviewerConfig(enabled=True),
        github=github,
        owner="owner",
        repo="repo",
        pr_number=1,
        tracking=tracking,
    )

    assert not outcome.success
    assert outcome.dispatched
    assert "GITHUB_TOKEN" in outcome.error
    assert tracking.auto_fix_attempts == 1


@pytest.mark.asyncio
async def test_dispatch_auto_fix_deterministic_lint_no_changes_returns_failure(monkeypatch):
    """When lint produces no diff, outcome.success=False with descriptive detail."""
    import caretaker.pr_reviewer.auto_fix as _af_mod
    import caretaker.pr_reviewer.backends._workdir as _wd
    from caretaker.config import PRReviewerConfig
    from caretaker.state.models import TrackedPR

    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setattr(_wd, "prepare_workdir", AsyncMock(return_value=("/tmp/fake", MagicMock())))
    monkeypatch.setattr(_wd, "cleanup_workdir", MagicMock())
    monkeypatch.setattr(_af_mod, "run_deterministic_lint", AsyncMock(return_value=False))

    github = MagicMock()
    github.upsert_issue_comment = AsyncMock()
    tracking = TrackedPR(number=2)

    outcome = await _auto_fix.dispatch_auto_fix(
        decision=_make_dispatch_decision(),
        pr_url="https://github.com/owner/repo/pull/2",
        head_branch="feature/lint",
        review=_make_dispatch_review(),
        config=PRReviewerConfig(enabled=True),
        github=github,
        owner="owner",
        repo="repo",
        pr_number=2,
        tracking=tracking,
    )

    assert outcome.dispatched
    assert not outcome.success
    assert "no changes" in outcome.detail
    assert tracking.auto_fix_attempts == 1
    # Status comment still posted so the author knows nothing changed.
    github.upsert_issue_comment.assert_awaited_once()


@pytest.mark.asyncio
async def test_dispatch_auto_fix_deterministic_lint_success_updates_tracking(monkeypatch):
    """Successful lint fix: tracking gets the new HEAD SHA, outcome.success=True."""
    import caretaker.pr_reviewer.auto_fix as _af_mod
    import caretaker.pr_reviewer.backends._workdir as _wd
    from caretaker.config import PRReviewerConfig
    from caretaker.state.models import TrackedPR

    new_sha = "abc123def456abc1"

    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setattr(_wd, "prepare_workdir", AsyncMock(return_value=("/tmp/fake", MagicMock())))
    monkeypatch.setattr(_wd, "cleanup_workdir", MagicMock())
    monkeypatch.setattr(_af_mod, "run_deterministic_lint", AsyncMock(return_value=True))
    monkeypatch.setattr(_af_mod, "commit_and_push", AsyncMock(return_value=new_sha))

    github = MagicMock()
    github.upsert_issue_comment = AsyncMock()
    tracking = TrackedPR(number=3)

    outcome = await _auto_fix.dispatch_auto_fix(
        decision=_make_dispatch_decision(),
        pr_url="https://github.com/owner/repo/pull/3",
        head_branch="feature/lint",
        review=_make_dispatch_review(),
        config=PRReviewerConfig(enabled=True),
        github=github,
        owner="owner",
        repo="repo",
        pr_number=3,
        tracking=tracking,
    )

    assert outcome.dispatched
    assert outcome.success
    assert outcome.new_head_sha == new_sha
    assert tracking.auto_fix_last_head_sha == new_sha
    assert tracking.auto_fix_attempts == 1
    github.upsert_issue_comment.assert_awaited_once()


# ── observability wire-up ────────────────────────────────────────────────────


def _read_dispatch_counter(repo: str, backend: str, category: str, outcome: str) -> float:
    from caretaker.observability.metrics import REGISTRY, get_service_label

    val = REGISTRY.get_sample_value(
        "caretaker_auto_fix_dispatch_total",
        {
            "service": get_service_label(),
            "repo": repo,
            "backend": backend,
            "category": category,
            "outcome": outcome,
        },
    )
    return 0.0 if val is None else float(val)


async def test_decide_auto_fix_records_skipped_when_disabled():
    """When the gate fails, ``skipped`` is recorded once with backend=none."""
    from caretaker.config import AutoFixConfig

    before = _read_dispatch_counter("owner/repo", "none", "none", "skipped")
    cfg = AutoFixConfig(enabled=False)
    review = ReviewResult(summary="x", verdict="REQUEST_CHANGES", comments=[])
    await _auto_fix.decide_auto_fix(
        review=review,
        config=cfg,
        pr_author="bot[bot]",
        pr_labels=[],
        tracking=_make_tracking(),
        repo="owner/repo",
    )
    after = _read_dispatch_counter("owner/repo", "none", "none", "skipped")
    assert after >= before + 1


async def test_decide_auto_fix_records_skipped_when_verdict_not_request_changes():
    before = _read_dispatch_counter("owner/repo", "none", "none", "skipped")
    cfg = _make_config()
    review = ReviewResult(summary="x", verdict="APPROVE", comments=[])
    await _auto_fix.decide_auto_fix(
        review=review,
        config=cfg,
        pr_author="bot[bot]",
        pr_labels=[],
        tracking=_make_tracking(),
        repo="owner/repo",
    )
    after = _read_dispatch_counter("owner/repo", "none", "none", "skipped")
    assert after >= before + 1


@pytest.mark.asyncio
async def test_dispatch_auto_fix_records_dispatched_success(monkeypatch):
    """A successful lint fix records ``dispatched_success``."""
    import caretaker.pr_reviewer.auto_fix as _af_mod
    import caretaker.pr_reviewer.backends._workdir as _wd
    from caretaker.config import PRReviewerConfig
    from caretaker.state.models import TrackedPR

    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setattr(_wd, "prepare_workdir", AsyncMock(return_value=("/tmp/fake", MagicMock())))
    monkeypatch.setattr(_wd, "cleanup_workdir", MagicMock())
    monkeypatch.setattr(_af_mod, "run_deterministic_lint", AsyncMock(return_value=True))
    monkeypatch.setattr(_af_mod, "commit_and_push", AsyncMock(return_value="newshaaa"))

    github = MagicMock()
    github.upsert_issue_comment = AsyncMock()

    before = _read_dispatch_counter(
        "owner/repo", "deterministic_lint", "lint", "dispatched_success"
    )
    await _auto_fix.dispatch_auto_fix(
        decision=_make_dispatch_decision(),
        pr_url="https://github.com/owner/repo/pull/9",
        head_branch="feature/lint",
        review=_make_dispatch_review(),
        config=PRReviewerConfig(enabled=True),
        github=github,
        owner="owner",
        repo="repo",
        pr_number=9,
        tracking=TrackedPR(number=9),
    )
    after = _read_dispatch_counter("owner/repo", "deterministic_lint", "lint", "dispatched_success")
    assert after >= before + 1


@pytest.mark.asyncio
async def test_dispatch_auto_fix_records_dispatched_fail_on_no_diff(monkeypatch):
    """A no-changes lint result records ``dispatched_fail``."""
    import caretaker.pr_reviewer.auto_fix as _af_mod
    import caretaker.pr_reviewer.backends._workdir as _wd
    from caretaker.config import PRReviewerConfig
    from caretaker.state.models import TrackedPR

    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setattr(_wd, "prepare_workdir", AsyncMock(return_value=("/tmp/fake", MagicMock())))
    monkeypatch.setattr(_wd, "cleanup_workdir", MagicMock())
    monkeypatch.setattr(_af_mod, "run_deterministic_lint", AsyncMock(return_value=False))

    github = MagicMock()
    github.upsert_issue_comment = AsyncMock()

    before = _read_dispatch_counter("owner/repo", "deterministic_lint", "lint", "dispatched_fail")
    await _auto_fix.dispatch_auto_fix(
        decision=_make_dispatch_decision(),
        pr_url="https://github.com/owner/repo/pull/10",
        head_branch="feature/lint",
        review=_make_dispatch_review(),
        config=PRReviewerConfig(enabled=True),
        github=github,
        owner="owner",
        repo="repo",
        pr_number=10,
        tracking=TrackedPR(number=10),
    )
    after = _read_dispatch_counter("owner/repo", "deterministic_lint", "lint", "dispatched_fail")
    assert after >= before + 1


@pytest.mark.asyncio
async def test_dispatch_auto_fix_records_dispatched_fail_on_exception(monkeypatch):
    """When the dispatcher hits an exception (no GITHUB_TOKEN), record ``dispatched_fail``."""
    from caretaker.config import PRReviewerConfig
    from caretaker.state.models import TrackedPR

    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    github = MagicMock()
    github.upsert_issue_comment = AsyncMock()

    before = _read_dispatch_counter("owner/repo", "deterministic_lint", "lint", "dispatched_fail")
    outcome = await _auto_fix.dispatch_auto_fix(
        decision=_make_dispatch_decision(),
        pr_url="https://github.com/owner/repo/pull/11",
        head_branch="feature/fix",
        review=_make_dispatch_review(),
        config=PRReviewerConfig(enabled=True),
        github=github,
        owner="owner",
        repo="repo",
        pr_number=11,
        tracking=TrackedPR(number=11),
    )
    assert not outcome.success
    after = _read_dispatch_counter("owner/repo", "deterministic_lint", "lint", "dispatched_fail")
    assert after >= before + 1
