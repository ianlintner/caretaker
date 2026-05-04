"""Tests for the auto-fix dispatcher (classify_issue_categories, decide_auto_fix)."""

from __future__ import annotations

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


def test_decide_disabled_returns_no_dispatch():
    from caretaker.config import AutoFixConfig

    cfg = AutoFixConfig(enabled=False)
    review = ReviewResult(summary="x", verdict="REQUEST_CHANGES", comments=[])
    d = _auto_fix.decide_auto_fix(
        review=review,
        config=cfg,
        pr_author="bot[bot]",
        pr_labels=[],
        tracking=_make_tracking(),
    )
    assert not d.should_dispatch


def test_decide_non_request_changes_returns_no_dispatch():
    cfg = _make_config()
    review = ReviewResult(summary="x", verdict="APPROVE", comments=[])
    d = _auto_fix.decide_auto_fix(
        review=review,
        config=cfg,
        pr_author="bot[bot]",
        pr_labels=[],
        tracking=_make_tracking(),
    )
    assert not d.should_dispatch


def test_decide_max_attempts_reached():
    cfg = _make_config(max_attempts=2)
    review = ReviewResult(summary="x", verdict="REQUEST_CHANGES", comments=[])
    tracking = _make_tracking(auto_fix_attempts=2)
    d = _auto_fix.decide_auto_fix(
        review=review,
        config=cfg,
        pr_author="bot[bot]",
        pr_labels=[],
        tracking=tracking,
    )
    assert not d.should_dispatch
    assert "max_attempts" in d.reason


def test_decide_author_eligible_dispatches():
    cfg = _make_config(allowed_authors=["copilot-swe-agent[bot]"])
    review = ReviewResult(
        summary="lint error",
        verdict="REQUEST_CHANGES",
        comments=[],
        issue_categories=["lint"],
    )
    d = _auto_fix.decide_auto_fix(
        review=review,
        config=cfg,
        pr_author="copilot-swe-agent[bot]",
        pr_labels=[],
        tracking=_make_tracking(),
    )
    assert d.should_dispatch
    assert d.backend == "deterministic_lint"


def test_decide_label_opt_in_eligible():
    cfg = _make_config(opt_in_label="caretaker:auto-fix")
    review = ReviewResult(
        summary="correctness bug",
        verdict="REQUEST_CHANGES",
        comments=[],
        issue_categories=["correctness"],
    )
    d = _auto_fix.decide_auto_fix(
        review=review,
        config=cfg,
        pr_author="human-dev",
        pr_labels=["caretaker:auto-fix"],
        tracking=_make_tracking(),
    )
    assert d.should_dispatch


def test_decide_unknown_author_no_label_denied():
    cfg = _make_config()
    review = ReviewResult(summary="x", verdict="REQUEST_CHANGES", comments=[])
    d = _auto_fix.decide_auto_fix(
        review=review,
        config=cfg,
        pr_author="random-human",
        pr_labels=[],
        tracking=_make_tracking(),
    )
    assert not d.should_dispatch


def test_decide_routes_lint_to_deterministic():
    cfg = _make_config(allowed_authors=["bot[bot]"])
    review = ReviewResult(
        summary="x",
        verdict="REQUEST_CHANGES",
        comments=[],
        issue_categories=["lint"],
    )
    d = _auto_fix.decide_auto_fix(
        review=review,
        config=cfg,
        pr_author="bot[bot]",
        pr_labels=[],
        tracking=_make_tracking(),
    )
    assert d.backend == "deterministic_lint"


def test_decide_falls_back_to_default_fixer():
    cfg = _make_config(allowed_authors=["bot[bot]"], default_fixer="claude_code_local")
    review = ReviewResult(
        summary="x",
        verdict="REQUEST_CHANGES",
        comments=[],
        issue_categories=[],
    )
    d = _auto_fix.decide_auto_fix(
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


def test_decide_always_run_heuristic_merges_categories():
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
    d = _auto_fix.decide_auto_fix(
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


def test_decide_always_run_heuristic_no_duplicates():
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
    d = _auto_fix.decide_auto_fix(
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

    from caretaker.pr_reviewer.backends._workdir import WorkdirError
    import caretaker.pr_reviewer.backends._workdir as _workdir_mod

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
