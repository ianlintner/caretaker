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
