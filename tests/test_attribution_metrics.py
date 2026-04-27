"""Tests for attribution metric emission at state-save time.

Covers the classifier (``StateTracker.classify_pr_outcomes`` /
``classify_issue_outcomes``), the dedup of repeated saves, and the
Prometheus counter increments the save path produces.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from caretaker.observability import metrics as _metrics
from caretaker.state.models import (
    IssueTrackingState,
    OrchestratorState,
    PRTrackingState,
    TrackedIssue,
    TrackedPR,
)
from caretaker.state.tracker import StateTracker


def _make_tracker() -> StateTracker:
    github = AsyncMock()
    github.upsert_issue_comment = AsyncMock()
    tracker = StateTracker(github=github, owner="acme", repo="widgets")
    tracker._tracking_issue_number = 1
    return tracker


def _read_counter(counter: object, **labels: str) -> float:
    """Return the current float value of a Prometheus counter child."""
    sample = counter.labels(**labels)  # type: ignore[attr-defined]
    return float(sample._value.get())  # type: ignore[attr-defined]


# ── Classifier semantics ─────────────────────────────────────────────────


class TestClassifyPrOutcomes:
    def test_untouched_pr_produces_empty_set(self) -> None:
        pr = TrackedPR(number=1)
        assert StateTracker.classify_pr_outcomes(pr) == frozenset()

    def test_touched_only(self) -> None:
        pr = TrackedPR(number=1, caretaker_touched=True)
        assert StateTracker.classify_pr_outcomes(pr) == frozenset({"touched"})

    def test_merged_implies_touched_label(self) -> None:
        pr = TrackedPR(
            number=1,
            caretaker_touched=True,
            caretaker_merged=True,
            state=PRTrackingState.MERGED,
        )
        outcomes = StateTracker.classify_pr_outcomes(pr)
        assert outcomes == frozenset({"touched", "merged"})

    def test_operator_rescued(self) -> None:
        pr = TrackedPR(
            number=1,
            caretaker_touched=True,
            operator_intervened=True,
            state=PRTrackingState.REVIEW_PENDING,
        )
        outcomes = StateTracker.classify_pr_outcomes(pr)
        assert "operator_rescued" in outcomes
        assert "touched" in outcomes

    def test_abandoned_only_when_escalated_without_rescue(self) -> None:
        escalated_no_rescue = TrackedPR(
            number=1,
            caretaker_touched=True,
            state=PRTrackingState.ESCALATED,
        )
        escalated_rescued = TrackedPR(
            number=2,
            caretaker_touched=True,
            state=PRTrackingState.ESCALATED,
            operator_intervened=True,
        )
        assert "abandoned" in StateTracker.classify_pr_outcomes(escalated_no_rescue)
        assert "abandoned" not in StateTracker.classify_pr_outcomes(escalated_rescued)

    def test_closed_unmerged_requires_caretaker_touch(self) -> None:
        closed_by_caretaker = TrackedPR(
            number=1,
            caretaker_touched=True,
            state=PRTrackingState.CLOSED,
        )
        closed_externally = TrackedPR(
            number=2,
            caretaker_touched=False,
            state=PRTrackingState.CLOSED,
        )
        assert "closed_unmerged" in StateTracker.classify_pr_outcomes(closed_by_caretaker)
        assert "closed_unmerged" not in StateTracker.classify_pr_outcomes(closed_externally)


class TestClassifyIssueOutcomes:
    def test_triaged(self) -> None:
        issue = TrackedIssue(number=1, caretaker_touched=True)
        assert "triaged" in StateTracker.classify_issue_outcomes(issue)

    def test_closed_by_caretaker(self) -> None:
        issue = TrackedIssue(
            number=1,
            caretaker_touched=True,
            caretaker_closed=True,
            state=IssueTrackingState.CLOSED,
        )
        assert "closed_by_caretaker" in StateTracker.classify_issue_outcomes(issue)

    def test_stale_closed_is_distinct_bucket(self) -> None:
        issue = TrackedIssue(
            number=1,
            caretaker_touched=True,
            caretaker_closed=True,
            state=IssueTrackingState.STALE,
        )
        outcomes = StateTracker.classify_issue_outcomes(issue)
        assert "stale_closed" in outcomes
        assert "closed_by_caretaker" not in outcomes

    def test_closed_by_operator(self) -> None:
        # Caretaker touched (e.g. triaged), but the close event came from
        # a human — classified as operator close for the rollup.
        issue = TrackedIssue(
            number=1,
            caretaker_touched=True,
            caretaker_closed=False,
            state=IssueTrackingState.CLOSED,
        )
        assert "closed_by_operator" in StateTracker.classify_issue_outcomes(issue)


# ── Emission integration ─────────────────────────────────────────────────


@pytest.mark.asyncio
class TestEmissionAtSave:
    async def test_save_emits_outcome_counters(self) -> None:
        tracker = _make_tracker()
        baseline_touched = _read_counter(
            _metrics.CARETAKER_PR_OUTCOME_TOTAL,
            service=_metrics.get_service_label(),
            repo="acme/widgets",
            outcome="touched",
        )
        baseline_merged = _read_counter(
            _metrics.CARETAKER_PR_OUTCOME_TOTAL,
            service=_metrics.get_service_label(),
            repo="acme/widgets",
            outcome="merged",
        )
        tracker._state = OrchestratorState(
            tracked_prs={
                42: TrackedPR(
                    number=42,
                    caretaker_touched=True,
                    caretaker_merged=True,
                    state=PRTrackingState.MERGED,
                    last_caretaker_action_at=datetime.now(UTC),
                )
            }
        )
        await tracker.save()

        assert (
            _read_counter(
                _metrics.CARETAKER_PR_OUTCOME_TOTAL,
                service=_metrics.get_service_label(),
                repo="acme/widgets",
                outcome="touched",
            )
            == baseline_touched + 1
        )
        assert (
            _read_counter(
                _metrics.CARETAKER_PR_OUTCOME_TOTAL,
                service=_metrics.get_service_label(),
                repo="acme/widgets",
                outcome="merged",
            )
            == baseline_merged + 1
        )

    async def test_repeated_saves_do_not_double_count(self) -> None:
        tracker = _make_tracker()
        baseline = _read_counter(
            _metrics.CARETAKER_PR_OUTCOME_TOTAL,
            service=_metrics.get_service_label(),
            repo="acme/widgets",
            outcome="touched",
        )
        pr = TrackedPR(
            number=77,
            caretaker_touched=True,
            last_caretaker_action_at=datetime.now(UTC),
        )
        tracker._state = OrchestratorState(tracked_prs={77: pr})

        for _ in range(3):
            await tracker.save()

        # The outcome set didn't change across saves, so the counter
        # only increments once.
        assert (
            _read_counter(
                _metrics.CARETAKER_PR_OUTCOME_TOTAL,
                service=_metrics.get_service_label(),
                repo="acme/widgets",
                outcome="touched",
            )
            == baseline + 1
        )

    async def test_intervention_reasons_emit_counter(self) -> None:
        tracker = _make_tracker()
        baseline = _read_counter(
            _metrics.CARETAKER_OPERATOR_INTERVENTION_TOTAL,
            service=_metrics.get_service_label(),
            repo="acme/widgets",
            reason="manual_merge",
        )
        pr = TrackedPR(
            number=1,
            caretaker_touched=True,
            operator_intervened=True,
            intervention_reasons=["manual_merge"],
            last_caretaker_action_at=datetime.now(UTC),
        )
        tracker._state = OrchestratorState(tracked_prs={1: pr})

        await tracker.save()

        assert (
            _read_counter(
                _metrics.CARETAKER_OPERATOR_INTERVENTION_TOTAL,
                service=_metrics.get_service_label(),
                repo="acme/widgets",
                reason="manual_merge",
            )
            == baseline + 1
        )

        # Second save with the same reason list is a no-op
        await tracker.save()
        assert (
            _read_counter(
                _metrics.CARETAKER_OPERATOR_INTERVENTION_TOTAL,
                service=_metrics.get_service_label(),
                repo="acme/widgets",
                reason="manual_merge",
            )
            == baseline + 1
        )
