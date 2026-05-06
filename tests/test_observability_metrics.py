"""Tests for the PR-reviewer observability helpers (phase 1A).

Covers the five recorder helpers added to ``caretaker.observability.metrics``:

  * :func:`record_pr_review_outcome` / :func:`observe_pr_review_duration`
  * :func:`record_complexity_tier`
  * :func:`record_opencode_invocation`
  * :func:`record_auto_fix_dispatch`

Each test reads back the underlying counter via ``REGISTRY.get_sample_value``
so the assertions exercise the same path Prometheus uses on a scrape.
"""

from __future__ import annotations

from caretaker.observability.metrics import (
    REGISTRY,
    get_service_label,
    observe_pr_review_duration,
    record_auto_fix_dispatch,
    record_complexity_tier,
    record_opencode_invocation,
    record_pr_review_outcome,
)


def _service() -> str:
    return get_service_label()


# ── record_pr_review_outcome ──────────────────────────────────────────


def test_record_pr_review_outcome_increments_counter() -> None:
    record_pr_review_outcome(
        repo="owner/repo", backend="opencode_local", tier="standard", verdict="APPROVE"
    )
    value = REGISTRY.get_sample_value(
        "caretaker_pr_review_outcome_total",
        {
            "service": _service(),
            "repo": "owner/repo",
            "backend": "opencode_local",
            "tier": "standard",
            "verdict": "APPROVE",
        },
    )
    assert value is not None
    assert value >= 1


def test_record_pr_review_outcome_unknown_tier_falls_back_to_other() -> None:
    """Unknown enum values land in the ``other`` bucket — no new series."""
    record_pr_review_outcome(
        repo="owner/repo",
        backend="opencode_local",
        tier="ULTRA-MEGA",  # not in COMPLEXITY_TIERS
        verdict="APPROVE",
    )
    value = REGISTRY.get_sample_value(
        "caretaker_pr_review_outcome_total",
        {
            "service": _service(),
            "repo": "owner/repo",
            "backend": "opencode_local",
            "tier": "other",
            "verdict": "APPROVE",
        },
    )
    assert value is not None
    assert value >= 1


def test_record_pr_review_outcome_unknown_verdict_falls_back_to_other() -> None:
    record_pr_review_outcome(repo="owner/repo", backend="inline", tier="trivial", verdict="WAFFLE")
    value = REGISTRY.get_sample_value(
        "caretaker_pr_review_outcome_total",
        {
            "service": _service(),
            "repo": "owner/repo",
            "backend": "inline",
            "tier": "trivial",
            "verdict": "other",
        },
    )
    assert value is not None
    assert value >= 1


# ── observe_pr_review_duration ────────────────────────────────────────


def test_observe_pr_review_duration_records_histogram_count() -> None:
    observe_pr_review_duration(backend="opencode_local", tier="simple", seconds=2.5)
    count = REGISTRY.get_sample_value(
        "caretaker_pr_review_duration_seconds_count",
        {"service": _service(), "backend": "opencode_local", "tier": "simple"},
    )
    assert count is not None
    assert count >= 1


def test_observe_pr_review_duration_unknown_tier_records_under_other() -> None:
    observe_pr_review_duration(backend="opencode_local", tier="not-a-tier", seconds=1.0)
    count = REGISTRY.get_sample_value(
        "caretaker_pr_review_duration_seconds_count",
        {"service": _service(), "backend": "opencode_local", "tier": "other"},
    )
    assert count is not None
    assert count >= 1


# ── record_complexity_tier ────────────────────────────────────────────


def test_record_complexity_tier_increments_counter() -> None:
    record_complexity_tier(tier="trivial", source="fast_path")
    value = REGISTRY.get_sample_value(
        "caretaker_complexity_classifier_tier_total",
        {"service": _service(), "tier": "trivial", "source": "fast_path"},
    )
    assert value is not None
    assert value >= 1


def test_record_complexity_tier_unknown_source_falls_back_to_other() -> None:
    record_complexity_tier(tier="trivial", source="from_outer_space")
    value = REGISTRY.get_sample_value(
        "caretaker_complexity_classifier_tier_total",
        {"service": _service(), "tier": "trivial", "source": "other"},
    )
    assert value is not None
    assert value >= 1


# ── record_opencode_invocation ────────────────────────────────────────


def test_record_opencode_invocation_increments_counter() -> None:
    record_opencode_invocation(
        model="openrouter/anthropic/claude-sonnet-4.5", mode="review", outcome="ok"
    )
    value = REGISTRY.get_sample_value(
        "caretaker_opencode_invocation_total",
        {
            "service": _service(),
            "model": "openrouter/anthropic/claude-sonnet-4.5",
            "mode": "review",
            "outcome": "ok",
        },
    )
    assert value is not None
    assert value >= 1


def test_record_opencode_invocation_unknown_outcome_falls_back_to_other() -> None:
    record_opencode_invocation(model="some/model", mode="review", outcome="weird-status")
    value = REGISTRY.get_sample_value(
        "caretaker_opencode_invocation_total",
        {
            "service": _service(),
            "model": "some/model",
            "mode": "review",
            "outcome": "other",
        },
    )
    assert value is not None
    assert value >= 1


def test_record_opencode_invocation_parse_fallback_outcome() -> None:
    """The ``parse_fallback`` outcome is recorded by _parse_review_payload."""
    record_opencode_invocation(model="<unknown>", mode="<unknown>", outcome="parse_fallback")
    value = REGISTRY.get_sample_value(
        "caretaker_opencode_invocation_total",
        {
            "service": _service(),
            "model": "<unknown>",
            "mode": "other",  # "<unknown>" not in OPENCODE_MODES
            "outcome": "parse_fallback",
        },
    )
    assert value is not None
    assert value >= 1


# ── record_auto_fix_dispatch ──────────────────────────────────────────


def test_record_auto_fix_dispatch_increments_counter() -> None:
    record_auto_fix_dispatch(
        repo="owner/repo",
        backend="deterministic_lint",
        category="lint",
        outcome="dispatched_success",
    )
    value = REGISTRY.get_sample_value(
        "caretaker_auto_fix_dispatch_total",
        {
            "service": _service(),
            "repo": "owner/repo",
            "backend": "deterministic_lint",
            "category": "lint",
            "outcome": "dispatched_success",
        },
    )
    assert value is not None
    assert value >= 1


def test_record_auto_fix_dispatch_skipped_with_none_backend() -> None:
    record_auto_fix_dispatch(repo="owner/repo", backend="none", category="none", outcome="skipped")
    value = REGISTRY.get_sample_value(
        "caretaker_auto_fix_dispatch_total",
        {
            "service": _service(),
            "repo": "owner/repo",
            "backend": "none",
            "category": "none",
            "outcome": "skipped",
        },
    )
    assert value is not None
    assert value >= 1


def test_record_auto_fix_dispatch_unknown_outcome_falls_back_to_other() -> None:
    record_auto_fix_dispatch(
        repo="owner/repo",
        backend="claude_code_local",
        category="security",
        outcome="not-a-real-outcome",
    )
    value = REGISTRY.get_sample_value(
        "caretaker_auto_fix_dispatch_total",
        {
            "service": _service(),
            "repo": "owner/repo",
            "backend": "claude_code_local",
            "category": "security",
            "outcome": "other",
        },
    )
    assert value is not None
    assert value >= 1


# ── enum constants are exported ──────────────────────────────────────


def test_bounded_enums_are_exported() -> None:
    """Ensure the enum tuples are reachable from the public namespace."""
    from caretaker.observability import metrics as m

    assert "trivial" in m.COMPLEXITY_TIERS
    assert "fast_path" in m.CLASSIFIER_SOURCES
    assert "review" in m.OPENCODE_MODES
    assert "ok" in m.OPENCODE_OUTCOMES
    assert "dispatched_success" in m.AUTO_FIX_OUTCOMES
    assert "APPROVE" in m.REVIEW_VERDICTS
