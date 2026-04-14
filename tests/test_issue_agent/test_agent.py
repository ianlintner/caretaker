"""Tests for IssueAgent run loop and lifecycle behavior."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from caretaker.config import IssueAgentConfig
from caretaker.github_client.models import Issue, PRState, PullRequest, User
from caretaker.issue_agent.agent import IssueAgent
from caretaker.state.models import IssueTrackingState, TrackedIssue


def make_issue(
    number: int,
    title: str,
    body: str,
    state: str = "open",
    assignees: list[User] | None = None,
    updated_at: datetime | None = None,
) -> Issue:
    return Issue(
        number=number,
        title=title,
        body=body,
        state=state,
        user=User(login="reporter", id=10, type="User"),
        assignees=assignees or [],
        updated_at=updated_at,
    )


def make_pr(number: int, title: str, body: str) -> PullRequest:
    return PullRequest(
        number=number,
        title=title,
        body=body,
        state=PRState.OPEN,
        user=User(login="copilot[bot]", id=1, type="Bot"),
    )


@pytest.mark.asyncio
class TestIssueAgent:
    async def test_stale_issue_is_closed(self) -> None:
        github = AsyncMock()
        old = datetime.now(UTC) - timedelta(days=40)
        github.list_issues.return_value = [
            make_issue(1, "Needs follow-up", "still broken", updated_at=old),
        ]
        github.list_pull_requests.return_value = []

        agent = IssueAgent(
            github=github,
            owner="o",
            repo="r",
            config=IssueAgentConfig(auto_close_stale_days=30),
        )

        report, tracked = await agent.run({})

        assert 1 in report.closed
        assert tracked[1].state == IssueTrackingState.STALE
        github.update_issue.assert_awaited_once()

    async def test_duplicate_issue_is_closed(self) -> None:
        github = AsyncMock()
        issue = make_issue(2, "Duplicate bug", "duplicate of #1")
        issue.labels = []
        github.list_issues.return_value = [issue]
        github.list_pull_requests.return_value = []

        agent = IssueAgent(
            github=github,
            owner="o",
            repo="r",
            config=IssueAgentConfig(),
        )

        report, tracked = await agent.run({})

        assert 2 in report.closed
        assert tracked[2].state == IssueTrackingState.CLOSED

    async def test_linked_pr_sets_pr_opened(self) -> None:
        github = AsyncMock()
        issue = make_issue(
            3,
            "Feature request",
            "please add a small feature",
            assignees=[User(login="copilot", id=22, type="Bot")],
        )
        github.list_issues.return_value = [issue]
        github.list_pull_requests.return_value = [
            make_pr(77, "Implement feature", "Fixes #3"),
        ]

        agent = IssueAgent(
            github=github,
            owner="o",
            repo="r",
            config=IssueAgentConfig(
                auto_assign_bugs=False,
                auto_assign_features=False,
                auto_close_questions=False,
            ),
        )

        _report, tracked = await agent.run({3: TrackedIssue(number=3)})

        assert tracked[3].state == IssueTrackingState.PR_OPENED
        assert tracked[3].assigned_pr == 77
