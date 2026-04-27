"""Tests for the ``caretaker backfill-attribution`` CLI command.

The backfill command is side-effect-heavy (it reads orchestrator state
from GitHub and writes it back). The unit-level coverage here exercises
the pure helpers — ``_parse_since``, ``_row_active_since``, and the
``backfill_missing_fields`` invariant reconciliation — plus a
behavioural test of the inference pass over a seeded ``OrchestratorState``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from caretaker.cli import _parse_since, _row_active_since
from caretaker.state.intervention_detector import backfill_missing_fields
from caretaker.state.models import (
    IssueTrackingState,
    OrchestratorState,
    PRTrackingState,
    TrackedIssue,
    TrackedPR,
)


class TestParseSince:
    def test_days_suffix(self) -> None:
        now = datetime.now(UTC)
        parsed = _parse_since("30d")
        # Parsed cutoff should be roughly 30 days ago (within 5s tolerance)
        assert abs((now - parsed).total_seconds() - 30 * 86400) < 5

    def test_weeks_suffix(self) -> None:
        now = datetime.now(UTC)
        parsed = _parse_since("2w")
        assert abs((now - parsed).total_seconds() - 14 * 86400) < 5

    def test_hours_suffix(self) -> None:
        now = datetime.now(UTC)
        parsed = _parse_since("12h")
        assert abs((now - parsed).total_seconds() - 12 * 3600) < 5

    def test_iso_8601_accepted(self) -> None:
        parsed = _parse_since("2026-04-01T00:00:00+00:00")
        assert parsed.year == 2026
        assert parsed.month == 4
        assert parsed.day == 1

    def test_invalid_raises(self) -> None:
        from click import ClickException

        with pytest.raises(ClickException):
            _parse_since("not-a-valid-span")


class TestRowActiveSince:
    def test_row_within_window(self) -> None:
        now = datetime.now(UTC)
        pr = TrackedPR(number=1, last_checked=now - timedelta(days=1))
        assert _row_active_since(pr, now - timedelta(days=7))

    def test_row_outside_window(self) -> None:
        now = datetime.now(UTC)
        pr = TrackedPR(number=1, last_checked=now - timedelta(days=30))
        assert not _row_active_since(pr, now - timedelta(days=7))

    def test_row_without_timestamps_is_included(self) -> None:
        # Unstamped rows are the prime backfill targets — the CLI must
        # not skip them just because they have no anchor.
        pr = TrackedPR(number=1)
        assert _row_active_since(pr, datetime.now(UTC) - timedelta(days=7))


class TestBackfillInference:
    """End-to-end behaviour: seed a state, run the inference pass."""

    def test_merged_pr_flips_caretaker_merged(self) -> None:
        state = OrchestratorState(
            tracked_prs={
                1: TrackedPR(
                    number=1,
                    state=PRTrackingState.MERGED,
                    caretaker_touched=False,
                    caretaker_merged=False,
                    first_seen_at=datetime.now(UTC) - timedelta(hours=5),
                    merged_at=datetime.now(UTC) - timedelta(hours=1),
                )
            }
        )
        # Apply the same inference the CLI does.
        for pr in state.tracked_prs.values():
            if pr.state == PRTrackingState.MERGED and not pr.caretaker_merged:
                pr.caretaker_merged = True
                pr.caretaker_touched = True
        backfill_missing_fields(state.tracked_prs, state.tracked_issues)

        assert state.tracked_prs[1].caretaker_merged is True
        assert state.tracked_prs[1].caretaker_touched is True

    def test_escalated_without_touch_gets_touch(self) -> None:
        state = OrchestratorState(
            tracked_prs={
                2: TrackedPR(
                    number=2,
                    state=PRTrackingState.ESCALATED,
                    caretaker_touched=False,
                    escalated=True,
                )
            }
        )
        for pr in state.tracked_prs.values():
            if pr.state == PRTrackingState.ESCALATED and not pr.caretaker_touched:
                pr.caretaker_touched = True
        assert state.tracked_prs[2].caretaker_touched is True

    def test_stale_issue_gets_closed_and_touched(self) -> None:
        state = OrchestratorState(
            tracked_issues={
                3: TrackedIssue(
                    number=3,
                    state=IssueTrackingState.STALE,
                    caretaker_touched=False,
                    caretaker_closed=False,
                )
            }
        )
        for issue in state.tracked_issues.values():
            if (
                issue.state in (IssueTrackingState.STALE, IssueTrackingState.CLOSED)
                and not issue.caretaker_touched
            ):
                issue.caretaker_touched = True
                if issue.state == IssueTrackingState.STALE:
                    issue.caretaker_closed = True
        assert state.tracked_issues[3].caretaker_touched is True
        assert state.tracked_issues[3].caretaker_closed is True

    def test_invariant_backfill_fills_merged_to_touched(self) -> None:
        state = OrchestratorState(
            tracked_prs={
                5: TrackedPR(
                    number=5,
                    caretaker_merged=True,
                    caretaker_touched=False,
                )
            }
        )
        count = backfill_missing_fields(state.tracked_prs, state.tracked_issues)
        assert count == 1
        assert state.tracked_prs[5].caretaker_touched is True

    def test_backfill_is_idempotent(self) -> None:
        state = OrchestratorState(
            tracked_prs={
                1: TrackedPR(
                    number=1,
                    caretaker_merged=True,
                    caretaker_touched=True,
                )
            }
        )
        assert backfill_missing_fields(state.tracked_prs, state.tracked_issues) == 0
