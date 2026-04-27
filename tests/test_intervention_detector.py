"""Unit tests for :mod:`caretaker.state.intervention_detector`.

The detector is pure and deterministic: for a given tracked-row snapshot
and event stream, it must return the same :class:`DetectionResult` every
time. These tests exercise the action-cutoff semantics, actor
classification, label filtering, deduplication, and the monotonic
``apply_*`` helpers.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from caretaker.state.intervention_detector import (
    DetectionResult,
    InterventionEvent,
    apply_issue_detection,
    apply_pr_detection,
    backfill_missing_fields,
    detect_issue_intervention,
    detect_pr_intervention,
)
from caretaker.state.models import (
    IssueTrackingState,
    PRTrackingState,
    TrackedIssue,
    TrackedPR,
)


def _now() -> datetime:
    return datetime(2026, 4, 21, 12, 0, tzinfo=UTC)


def _pr_with_touch(minutes_ago: int = 30) -> TrackedPR:
    return TrackedPR(
        number=1,
        state=PRTrackingState.REVIEW_PENDING,
        caretaker_touched=True,
        last_caretaker_action_at=_now() - timedelta(minutes=minutes_ago),
    )


def _issue_with_touch(minutes_ago: int = 30) -> TrackedIssue:
    return TrackedIssue(
        number=1,
        state=IssueTrackingState.TRIAGED,
        caretaker_touched=True,
        last_caretaker_action_at=_now() - timedelta(minutes=minutes_ago),
    )


# ── PR detection ─────────────────────────────────────────────────────────


class TestDetectPrIntervention:
    def test_no_events_returns_empty_result(self) -> None:
        tracking = _pr_with_touch()
        result = detect_pr_intervention(tracking, [])
        assert result.intervened is False
        assert result.reasons == []

    def test_pr_never_touched_by_caretaker_is_not_an_intervention(self) -> None:
        # Caretaker never took an action — any human activity is normal
        # author behaviour, not a rescue.
        tracking = TrackedPR(number=1)
        events = [InterventionEvent(kind="commit", actor="alice", occurred_at=_now())]
        result = detect_pr_intervention(tracking, events)
        assert result.intervened is False

    def test_human_commit_after_caretaker_flags_intervention(self) -> None:
        tracking = _pr_with_touch(minutes_ago=30)
        # Human pushed a commit 10m after caretaker's last action
        events = [
            InterventionEvent(
                kind="commit",
                actor="alice",
                occurred_at=_now() - timedelta(minutes=20),
            )
        ]
        result = detect_pr_intervention(tracking, events)
        assert result.intervened is True
        assert result.reasons == ["commit_added"]

    def test_bot_action_is_not_an_intervention(self) -> None:
        tracking = _pr_with_touch()
        events = [
            InterventionEvent(
                kind="commit",
                actor="dependabot[bot]",
                occurred_at=_now() - timedelta(minutes=5),
            )
        ]
        result = detect_pr_intervention(tracking, events)
        assert result.intervened is False

    def test_event_before_last_caretaker_action_is_ignored(self) -> None:
        tracking = _pr_with_touch(minutes_ago=10)
        events = [
            # 20m ago is before caretaker's action 10m ago
            InterventionEvent(
                kind="commit",
                actor="alice",
                occurred_at=_now() - timedelta(minutes=20),
            )
        ]
        result = detect_pr_intervention(tracking, events)
        assert result.intervened is False

    def test_manual_merge_close_force_push_all_recorded(self) -> None:
        tracking = _pr_with_touch(minutes_ago=60)
        events = [
            InterventionEvent(
                kind="merged", actor="alice", occurred_at=_now() - timedelta(minutes=30)
            ),
            InterventionEvent(
                kind="closed", actor="alice", occurred_at=_now() - timedelta(minutes=25)
            ),
            InterventionEvent(
                kind="force_push", actor="alice", occurred_at=_now() - timedelta(minutes=20)
            ),
        ]
        result = detect_pr_intervention(tracking, events)
        assert result.intervened is True
        assert set(result.reasons) == {"manual_merge", "manual_close", "force_push"}

    def test_caretaker_own_labels_are_filtered(self) -> None:
        tracking = _pr_with_touch()
        events = [
            # Human applying a caretaker label is still caretaker territory
            InterventionEvent(
                kind="labeled",
                actor="alice",
                occurred_at=_now() - timedelta(minutes=5),
                label="maintainer:escalated",
            ),
        ]
        result = detect_pr_intervention(tracking, events)
        assert result.intervened is False

    def test_human_label_change_does_count(self) -> None:
        tracking = _pr_with_touch()
        events = [
            InterventionEvent(
                kind="labeled",
                actor="alice",
                occurred_at=_now() - timedelta(minutes=5),
                label="priority:high",
            ),
        ]
        result = detect_pr_intervention(tracking, events)
        assert result.intervened is True
        assert result.reasons == ["label_changed"]

    def test_reasons_are_deduplicated(self) -> None:
        tracking = _pr_with_touch(minutes_ago=60)
        events = [
            InterventionEvent(
                kind="commit", actor="alice", occurred_at=_now() - timedelta(minutes=30)
            ),
            InterventionEvent(
                kind="commit", actor="alice", occurred_at=_now() - timedelta(minutes=20)
            ),
            InterventionEvent(
                kind="commit", actor="bob", occurred_at=_now() - timedelta(minutes=10)
            ),
        ]
        result = detect_pr_intervention(tracking, events)
        assert result.reasons == ["commit_added"]

    def test_detector_is_idempotent(self) -> None:
        tracking = _pr_with_touch(minutes_ago=60)
        events = [
            InterventionEvent(
                kind="merged",
                actor="alice",
                occurred_at=_now() - timedelta(minutes=30),
            ),
        ]
        first = detect_pr_intervention(tracking, events)
        second = detect_pr_intervention(tracking, events)
        assert first == second


# ── Apply helpers ────────────────────────────────────────────────────────


class TestApplyPrDetection:
    def test_apply_flips_intervened_once(self) -> None:
        tracking = _pr_with_touch()
        result = DetectionResult(intervened=True, reasons=["manual_merge"])
        changed = apply_pr_detection(tracking, result)
        assert changed is True
        assert tracking.operator_intervened is True
        assert tracking.intervention_reasons == ["manual_merge"]

        # Re-applying the same result is a no-op
        changed_again = apply_pr_detection(tracking, result)
        assert changed_again is False
        assert tracking.intervention_reasons == ["manual_merge"]

    def test_apply_appends_new_reasons_without_duplicates(self) -> None:
        tracking = _pr_with_touch()
        apply_pr_detection(tracking, DetectionResult(intervened=True, reasons=["manual_merge"]))
        apply_pr_detection(
            tracking, DetectionResult(intervened=True, reasons=["manual_merge", "force_push"])
        )
        assert tracking.intervention_reasons == ["manual_merge", "force_push"]

    def test_empty_result_is_noop(self) -> None:
        tracking = _pr_with_touch()
        changed = apply_pr_detection(tracking, DetectionResult())
        assert changed is False
        assert tracking.operator_intervened is False
        assert tracking.intervention_reasons == []


# ── Issue flavour ─────────────────────────────────────────────────────────


class TestDetectIssueIntervention:
    def test_manual_close_after_caretaker_is_an_intervention(self) -> None:
        tracking = _issue_with_touch()
        events = [
            InterventionEvent(
                kind="closed",
                actor="alice",
                occurred_at=_now() - timedelta(minutes=5),
            )
        ]
        result = detect_issue_intervention(tracking, events)
        assert result.intervened is True
        assert result.reasons == ["manual_close"]

    def test_apply_to_issue_sets_flags(self) -> None:
        tracking = _issue_with_touch()
        apply_issue_detection(tracking, DetectionResult(intervened=True, reasons=["manual_close"]))
        assert tracking.operator_intervened is True
        assert tracking.intervention_reasons == ["manual_close"]


# ── Backfill invariants ──────────────────────────────────────────────────


class TestBackfillMissingFields:
    def test_merged_implies_touched(self) -> None:
        pr = TrackedPR(number=1, caretaker_merged=True, caretaker_touched=False)
        mutated = backfill_missing_fields({1: pr}, {})
        assert mutated == 1
        assert pr.caretaker_touched is True

    def test_issue_closed_implies_touched(self) -> None:
        issue = TrackedIssue(number=1, caretaker_closed=True, caretaker_touched=False)
        mutated = backfill_missing_fields({}, {1: issue})
        assert mutated == 1
        assert issue.caretaker_touched is True

    def test_already_coherent_is_noop(self) -> None:
        pr = TrackedPR(number=1, caretaker_merged=True, caretaker_touched=True)
        assert backfill_missing_fields({1: pr}, {}) == 0


@pytest.mark.parametrize(
    "kind,expected",
    [
        ("commit", "commit_added"),
        ("merged", "manual_merge"),
        ("closed", "manual_close"),
        ("force_push", "force_push"),
    ],
)
def test_reason_vocabulary_is_stable(kind: str, expected: str) -> None:
    """Guard against accidental shifts in the short-code vocabulary.

    The metric-label set is bounded by this vocabulary; renaming a
    reason silently would explode Prometheus cardinality.
    """
    tracking = _pr_with_touch()
    events = [
        InterventionEvent(kind=kind, actor="alice", occurred_at=_now() - timedelta(minutes=5))
    ]
    result = detect_pr_intervention(tracking, events)
    assert result.reasons == [expected]
