"""Tests for the Charlie janitorial agent."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from caretaker.charlie_agent.agent import CharlieAgent
from caretaker.github_client.models import Issue, Label, PRState, PullRequest, User


def _issue(
    number: int,
    *,
    title: str = "Regular issue",
    body: str = "",
    updated_days_ago: int = 2,
    labels: list[str] | None = None,
    assignees: list[User] | None = None,
    user: User | None = None,
) -> Issue:
    now = datetime.now(UTC)
    return Issue(
        number=number,
        title=title,
        body=body,
        state="open",
        user=user or User(login="app/github-actions", id=1, type="Bot"),
        labels=[Label(name=name) for name in (labels or [])],
        assignees=assignees or [],
        created_at=now - timedelta(days=updated_days_ago + 1),
        updated_at=now - timedelta(days=updated_days_ago),
        html_url=f"https://github.com/o/r/issues/{number}",
    )


def _pr(
    number: int,
    *,
    title: str = "Caretaker PR",
    body: str = "",
    updated_days_ago: int = 2,
    labels: list[str] | None = None,
    draft: bool = False,
    user: User | None = None,
) -> PullRequest:
    now = datetime.now(UTC)
    return PullRequest(
        number=number,
        title=title,
        body=body,
        state=PRState.OPEN,
        user=user or User(login="Copilot", id=2, type="Bot"),
        head_ref=f"feature/{number}",
        base_ref="main",
        mergeable=True,
        merged=False,
        draft=draft,
        labels=[Label(name=name) for name in (labels or [])],
        created_at=now - timedelta(days=updated_days_ago + 1),
        updated_at=now - timedelta(days=updated_days_ago),
        html_url=f"https://github.com/o/r/pull/{number}",
    )


def make_github(
    *,
    issues: list[Issue] | None = None,
    prs: list[PullRequest] | None = None,
) -> AsyncMock:
    gh = AsyncMock()
    gh.list_issues.return_value = issues or []
    gh.list_pull_requests.return_value = prs or []
    gh.add_issue_comment.return_value = None
    gh.update_issue.return_value = None
    return gh


class TestCharlieAgent:
    @pytest.mark.asyncio
    async def test_closes_duplicate_managed_issues_using_source_issue_key(self) -> None:
        canonical = _issue(
            10,
            body="<!-- caretaker:assignment -->\nSOURCE_ISSUE: #42",
            assignees=[User(login="Copilot", id=3, type="Bot")],
        )
        duplicate = _issue(
            11,
            body="<!-- caretaker:assignment -->\nSOURCE_ISSUE: #42",
        )
        gh = make_github(issues=[canonical, duplicate])

        report = await CharlieAgent(github=gh, owner="o", repo="r").run()

        assert report.managed_issues_seen == 2
        assert report.duplicate_issues_closed == 1
        assert report.issues_closed == 1
        gh.add_issue_comment.assert_awaited_once()
        gh.update_issue.assert_awaited_once_with("o", "r", 11, state="closed")

    @pytest.mark.asyncio
    async def test_closes_duplicate_managed_prs_using_fixes_key(self) -> None:
        canonical = _pr(20, body="Fixes #77")
        duplicate = _pr(21, body="Fixes #77", draft=True)
        gh = make_github(prs=[canonical, duplicate])

        report = await CharlieAgent(github=gh, owner="o", repo="r").run()

        assert report.managed_prs_seen == 2
        assert report.duplicate_prs_closed == 1
        assert report.prs_closed == 1
        gh.update_issue.assert_awaited_once_with("o", "r", 21, state="closed")

    @pytest.mark.asyncio
    async def test_closes_stale_managed_issue_after_short_window(self) -> None:
        stale_issue = _issue(
            30,
            title="[Maintainer] Follow-up cleanup",
            updated_days_ago=21,
        )
        gh = make_github(issues=[stale_issue])

        report = await CharlieAgent(github=gh, owner="o", repo="r", stale_days=14).run()

        assert report.stale_issues_closed == 1
        assert report.issues_closed == 1
        gh.update_issue.assert_awaited_once_with("o", "r", 30, state="closed")

    @pytest.mark.asyncio
    async def test_respects_exempt_labels_for_stale_cleanup(self) -> None:
        exempt_issue = _issue(
            31,
            title="[Maintainer] Escalated item",
            updated_days_ago=30,
            labels=["maintainer:escalated"],
        )
        gh = make_github(issues=[exempt_issue])

        report = await CharlieAgent(github=gh, owner="o", repo="r", stale_days=14).run()

        assert report.stale_issues_closed == 0
        assert report.issues_closed == 0
        gh.add_issue_comment.assert_not_awaited()
        gh.update_issue.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skips_stale_closure_for_copilot_assigned_issue(self) -> None:
        active_assignment = _issue(
            32,
            body="<!-- caretaker:assignment -->\nSOURCE_ISSUE: #99",
            updated_days_ago=30,
            assignees=[User(login="Copilot", id=4, type="Bot")],
        )
        gh = make_github(issues=[active_assignment])

        report = await CharlieAgent(github=gh, owner="o", repo="r", stale_days=14).run()

        assert report.stale_issues_closed == 0
        assert report.issues_closed == 0
        gh.update_issue.assert_not_awaited()
