"""Tests for orchestrator state-tracker comment idempotency.

Verifies the Sprint 2 fix that switched the tracking-issue comment writes
from append-per-run to upsert-by-marker. Pre-fix, every save() and
post_run_summary() call appended a new comment to the tracking issue,
producing unbounded growth (portfolio#121 hit 110 bot comments).
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from caretaker.state.models import RunSummary
from caretaker.state.tracker import (
    RUN_HISTORY_COMMENT_MARKER,
    STATE_COMMENT_MARKER,
    StateTracker,
)


def _summary(prs: int = 3, mode: str = "full") -> RunSummary:
    return RunSummary(
        run_at=datetime(2026, 4, 19, 12, 0, tzinfo=UTC),
        mode=mode,
        prs_monitored=prs,
        prs_merged=1,
    )


@pytest.fixture
def tracker_with_issue() -> StateTracker:
    github = AsyncMock()
    github.upsert_issue_comment = AsyncMock()
    tracker = StateTracker(github=github, owner="o", repo="r")
    tracker._tracking_issue_number = 99  # bypass create_tracking_issue
    return tracker


@pytest.mark.asyncio
class TestSaveUsesUpsert:
    async def test_save_calls_upsert_with_state_marker(
        self, tracker_with_issue: StateTracker
    ) -> None:
        tracker = tracker_with_issue
        await tracker.save(summary=_summary())

        tracker._github.upsert_issue_comment.assert_awaited_once()
        args = tracker._github.upsert_issue_comment.await_args.args
        assert args[:3] == ("o", "r", 99)
        assert args[3] == STATE_COMMENT_MARKER
        body = args[4]
        assert STATE_COMMENT_MARKER in body
        assert "Orchestrator State Update" in body
        assert "caretaker:causal" in body
        assert "source=state-tracker:orchestrator-state" in body

    async def test_repeated_saves_only_call_upsert(self, tracker_with_issue: StateTracker) -> None:
        """Five back-to-back saves must produce five upsert calls (not five posts)."""
        tracker = tracker_with_issue
        for _ in range(5):
            await tracker.save(summary=_summary())

        assert tracker._github.upsert_issue_comment.await_count == 5
        # All upsert calls used the state marker
        for call in tracker._github.upsert_issue_comment.await_args_list:
            assert call.args[3] == STATE_COMMENT_MARKER


@pytest.mark.asyncio
class TestPostRunSummaryUsesUpsert:
    async def test_post_run_summary_uses_history_marker(
        self, tracker_with_issue: StateTracker
    ) -> None:
        tracker = tracker_with_issue
        summary = _summary()
        await tracker.post_run_summary(summary)

        tracker._github.upsert_issue_comment.assert_awaited_once()
        args = tracker._github.upsert_issue_comment.await_args.args
        assert args[3] == RUN_HISTORY_COMMENT_MARKER
        body = args[4]
        assert RUN_HISTORY_COMMENT_MARKER in body
        assert "Maintainer Run History" in body
        assert "PRs monitored" in body
        assert "caretaker:causal" in body
        assert "source=state-tracker:run-history" in body

    async def test_history_keeps_at_most_10_runs(self, tracker_with_issue: StateTracker) -> None:
        tracker = tracker_with_issue
        for i in range(15):
            tracker._state.run_history.append(_summary(prs=i))

        await tracker.post_run_summary(_summary(prs=99))

        body = tracker._github.upsert_issue_comment.await_args.args[4]
        # Body should mention exactly 10 runs in the header
        assert "(last 10 runs)" in body

    async def test_history_includes_latest_run_first(
        self, tracker_with_issue: StateTracker
    ) -> None:
        tracker = tracker_with_issue
        # Pre-populate with two older runs so save+post pattern is meaningful
        tracker._state.run_history.append(_summary(prs=1))
        tracker._state.run_history.append(_summary(prs=2))

        latest = _summary(prs=999)
        await tracker.post_run_summary(latest)

        body = tracker._github.upsert_issue_comment.await_args.args[4]
        # The latest run renders first (newest-first ordering)
        idx_latest = body.find("999 PRs monitored")
        idx_one = body.find("1 PRs monitored")
        assert idx_latest != -1
        assert idx_one != -1
        assert idx_latest < idx_one


# ── Tracking-issue rotation on 403 comment-limit ─────────────────────────────


def _make_full_issue_error() -> Exception:
    """Return a GitHubAPIError that looks like GitHub's 'too many comments' 403."""
    from caretaker.github_client.api import GitHubAPIError

    return GitHubAPIError(
        403,
        '{"message":"Commenting is disabled on issues with more than 2500 comments",'
        '"documentation_url":"https://docs.github.com/rest","status":"403"}',
    )


@pytest.mark.asyncio
class TestTrackingIssueRotation:
    """save() and post_run_summary() must rotate to a new issue on 403 comment limit."""

    async def test_save_rotates_on_comment_limit_error(self) -> None:
        """save() should create a new tracking issue when the current one is full."""
        github = AsyncMock()
        # First upsert raises the 403; second (on new issue) succeeds.
        github.upsert_issue_comment = AsyncMock(side_effect=[_make_full_issue_error(), AsyncMock()])
        new_issue = AsyncMock()
        new_issue.number = 200
        github.update_issue = AsyncMock()
        tracker = StateTracker(github=github, owner="o", repo="r")
        tracker._tracking_issue_number = 99
        tracker._issues = AsyncMock()
        tracker._issues.create = AsyncMock(return_value=new_issue)

        await tracker.save(summary=_summary())

        # Old issue should have been closed
        github.update_issue.assert_awaited_once()
        close_args = github.update_issue.await_args
        assert close_args.args[2] == 99
        assert close_args.kwargs.get("state") == "closed"

        # A new tracking issue should have been created
        tracker._issues.create.assert_awaited_once()

        # The retry upsert must land on the new issue number
        assert tracker._tracking_issue_number == 200
        assert github.upsert_issue_comment.await_count == 2
        retry_call = github.upsert_issue_comment.await_args
        assert retry_call.args[2] == 200

    async def test_save_does_not_rotate_on_other_errors(self) -> None:
        """Non-comment-limit errors must propagate without rotation."""
        from caretaker.github_client.api import GitHubAPIError

        github = AsyncMock()
        github.upsert_issue_comment = AsyncMock(
            side_effect=GitHubAPIError(403, '{"message":"Resource not accessible"}')
        )
        github.update_issue = AsyncMock()
        tracker = StateTracker(github=github, owner="o", repo="r")
        tracker._tracking_issue_number = 99

        with pytest.raises(GitHubAPIError):
            await tracker.save(summary=_summary())

        # update_issue must NOT be called — no rotation
        github.update_issue.assert_not_awaited()

    async def test_post_run_summary_rotates_on_comment_limit_error(self) -> None:
        """post_run_summary() should rotate to a new issue on 403 comment limit."""
        github = AsyncMock()
        github.upsert_issue_comment = AsyncMock(side_effect=[_make_full_issue_error(), AsyncMock()])
        new_issue = AsyncMock()
        new_issue.number = 201
        github.update_issue = AsyncMock()
        tracker = StateTracker(github=github, owner="o", repo="r")
        tracker._tracking_issue_number = 99
        tracker._issues = AsyncMock()
        tracker._issues.create = AsyncMock(return_value=new_issue)

        await tracker.post_run_summary(_summary())

        github.update_issue.assert_awaited_once()
        assert tracker._tracking_issue_number == 201
        assert github.upsert_issue_comment.await_count == 2
