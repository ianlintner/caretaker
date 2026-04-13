"""Tests for the stale agent."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from caretaker.github_client.models import Issue, Label, PullRequest, User
from caretaker.stale_agent.agent import STALE_LABEL, StaleAgent


def _dt_str(days_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def _issue(
    number: int = 1,
    updated_days_ago: int = 30,
    labels: list[str] | None = None,
) -> Issue:
    return Issue(
        number=number,
        title=f"Issue #{number}",
        body="",
        state="open",
        user=User(login="dev", id=1),
        labels=[Label(name=n) for n in (labels or [])],
        assignees=[],
        updated_at=datetime.now(timezone.utc) - timedelta(days=updated_days_ago),
        html_url=f"https://github.com/o/r/issues/{number}",
    )


def _pr(
    number: int = 100,
    updated_days_ago: int = 30,
    merged: bool = False,
    head_ref: str = "feature/old",
    labels: list[str] | None = None,
    draft: bool = False,
) -> PullRequest:
    return PullRequest(
        number=number,
        title=f"PR #{number}",
        body="",
        state="closed" if merged else "open",
        user=User(login="dev", id=2),
        head_ref=head_ref,
        base_ref="main",
        merged=merged,
        draft=draft,
        labels=[Label(name=n) for n in (labels or [])],
        updated_at=datetime.now(timezone.utc) - timedelta(days=updated_days_ago),
        html_url=f"https://github.com/o/r/pull/{number}",
    )


def make_github(
    open_issues: list | None = None,
    open_prs: list | None = None,
    closed_prs: list | None = None,
) -> AsyncMock:
    gh = AsyncMock()
    gh.list_issues.return_value = open_issues or []
    gh.list_pull_requests.side_effect = lambda owner, repo, state="open": (
        open_prs or [] if state == "open" else closed_prs or []
    )
    gh.add_labels.return_value = None
    gh.add_issue_comment.return_value = None
    gh.update_issue.return_value = None
    gh.delete_branch.return_value = None
    return gh


# ── Stale issue warning ──────────────────────────────────────────────


class TestStaleIssueWarning:
    @pytest.mark.asyncio
    async def test_warns_issue_past_stale_days(self) -> None:
        issue = _issue(1, updated_days_ago=65)  # stale_days=60
        gh = make_github(open_issues=[issue])
        agent = StaleAgent(github=gh, owner="o", repo="r", stale_days=60)
        report = await agent.run()

        assert report.issues_warned == 1
        gh.add_labels.assert_awaited_once_with("o", "r", 1, [STALE_LABEL])
        gh.add_issue_comment.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_does_not_warn_recent_issue(self) -> None:
        issue = _issue(1, updated_days_ago=10)
        gh = make_github(open_issues=[issue])
        agent = StaleAgent(github=gh, owner="o", repo="r", stale_days=60)
        report = await agent.run()

        assert report.issues_warned == 0
        gh.add_labels.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_closes_already_stale_issue(self) -> None:
        # Issue is stale (has stale label) and updated 80 days ago; close_after=14
        issue = _issue(1, updated_days_ago=80, labels=[STALE_LABEL])
        gh = make_github(open_issues=[issue])
        agent = StaleAgent(github=gh, owner="o", repo="r", stale_days=60, close_after=14)
        report = await agent.run()

        assert report.issues_closed == 1
        gh.update_issue.assert_awaited_once()
        call_kwargs = gh.update_issue.call_args.kwargs
        assert call_kwargs.get("state") == "closed"

    @pytest.mark.asyncio
    async def test_exempt_label_prevents_stale_warning(self) -> None:
        # security:finding is in the exempt set
        issue = _issue(1, updated_days_ago=90, labels=["security:finding"])
        gh = make_github(open_issues=[issue])
        agent = StaleAgent(github=gh, owner="o", repo="r", stale_days=60)
        report = await agent.run()

        assert report.issues_warned == 0
        assert report.issues_closed == 0


# ── Stale PR handling ────────────────────────────────────────────────


class TestStalePRHandling:
    @pytest.mark.asyncio
    async def test_closes_stale_pr(self) -> None:
        stale_pr = _pr(101, updated_days_ago=80)
        gh = make_github(open_prs=[stale_pr])
        agent = StaleAgent(github=gh, owner="o", repo="r", stale_days=60, close_after=14)
        report = await agent.run()

        assert report.prs_closed == 1
        gh.update_issue.assert_awaited()

    @pytest.mark.asyncio
    async def test_skips_draft_prs(self) -> None:
        draft = _pr(102, updated_days_ago=90, draft=True)
        gh = make_github(open_prs=[draft])
        agent = StaleAgent(github=gh, owner="o", repo="r", stale_days=60)
        report = await agent.run()

        assert report.prs_closed == 0

    @pytest.mark.asyncio
    async def test_close_stale_prs_disabled(self) -> None:
        stale_pr = _pr(101, updated_days_ago=90)
        gh = make_github(open_prs=[stale_pr])
        agent = StaleAgent(
            github=gh, owner="o", repo="r", stale_days=60, close_stale_prs=False
        )
        report = await agent.run()

        assert report.prs_closed == 0


# ── Branch pruning ───────────────────────────────────────────────────


class TestBranchPruning:
    @pytest.mark.asyncio
    async def test_deletes_merged_branch(self) -> None:
        merged = _pr(200, merged=True, head_ref="feature/old-branch")
        gh = make_github(closed_prs=[merged])
        agent = StaleAgent(github=gh, owner="o", repo="r", delete_merged_branches=True)
        report = await agent.run()

        assert report.branches_deleted == 1
        gh.delete_branch.assert_awaited_once_with("o", "r", "feature/old-branch")

    @pytest.mark.asyncio
    async def test_skips_main_branch(self) -> None:
        merged = _pr(200, merged=True, head_ref="main")
        gh = make_github(closed_prs=[merged])
        agent = StaleAgent(github=gh, owner="o", repo="r", delete_merged_branches=True)
        report = await agent.run()

        assert report.branches_deleted == 0

    @pytest.mark.asyncio
    async def test_branch_pruning_disabled(self) -> None:
        merged = _pr(200, merged=True, head_ref="feature/old-branch")
        gh = make_github(closed_prs=[merged])
        agent = StaleAgent(
            github=gh, owner="o", repo="r", delete_merged_branches=False
        )
        report = await agent.run()

        assert report.branches_deleted == 0
        gh.delete_branch.assert_not_awaited()
