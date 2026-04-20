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

from caretaker.github_client.api import CommentingDisabledError
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


@pytest.mark.asyncio
class TestCommentingDisabledHandling:
    """Tests that verify graceful handling of GitHub's 2500-comment limit."""

    async def test_save_creates_fresh_issue_when_commenting_disabled(self) -> None:
        """save() must rotate to a new tracking issue when commenting is disabled."""
        github = AsyncMock()
        # First upsert call raises; second (on the new issue) succeeds.
        github.upsert_issue_comment = AsyncMock(
            side_effect=[
                CommentingDisabledError(
                    403,
                    "Commenting is disabled on issues with more than 2500 comments",
                ),
                AsyncMock(),
            ]
        )
        new_issue = AsyncMock()
        new_issue.number = 200
        github.create_issue = AsyncMock(return_value=new_issue)

        tracker = StateTracker(github=github, owner="o", repo="r")
        tracker._tracking_issue_number = 99

        # Patch _create_tracking_issue to simulate creating issue #200
        async def _fake_create() -> None:
            tracker._tracking_issue_number = 200

        tracker._create_tracking_issue = _fake_create  # type: ignore[method-assign]

        await tracker.save(summary=_summary())

        # State was ultimately saved on the fresh issue
        assert tracker._tracking_issue_number == 200
        assert github.upsert_issue_comment.await_count == 2
        second_call = github.upsert_issue_comment.await_args_list[1]
        assert second_call.args[2] == 200

    async def test_post_run_summary_skips_when_commenting_disabled(self) -> None:
        """post_run_summary() must log a warning and not raise when commenting is disabled."""
        github = AsyncMock()
        github.upsert_issue_comment = AsyncMock(
            side_effect=CommentingDisabledError(
                403, "Commenting is disabled on issues with more than 2500 comments"
            )
        )
        tracker = StateTracker(github=github, owner="o", repo="r")
        tracker._tracking_issue_number = 99

        # Should not raise
        await tracker.post_run_summary(_summary())

        github.upsert_issue_comment.assert_awaited_once()

    async def test_commenting_disabled_error_is_subclass_of_github_api_error(self) -> None:
        from caretaker.github_client.api import GitHubAPIError

        err = CommentingDisabledError(403, "msg")
        assert isinstance(err, GitHubAPIError)
        assert err.status_code == 403
