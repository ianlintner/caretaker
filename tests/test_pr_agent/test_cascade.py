"""Tests for the cross-entity cascade planner."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from caretaker.github_client.models import Issue, User
from caretaker.pr_agent.cascade import (
    CascadeAction,
    CascadeKind,
    apply_cascade,
    on_issue_closed_as_duplicate,
    on_pr_closed_unmerged,
    on_pr_merged,
    parse_linked_issues,
)
from caretaker.state.models import IssueTrackingState, TrackedIssue, TrackedPR
from tests.conftest import make_pr


class TestParseLinkedIssues:
    def test_fixes_keyword(self) -> None:
        assert parse_linked_issues("Fixes #42") == [42]

    def test_closes_plural_variants(self) -> None:
        body = "Resolves #1. Closed #2. Fix #3. resolves #4"
        assert parse_linked_issues(body) == [1, 2, 3, 4]

    def test_deduplicates(self) -> None:
        assert parse_linked_issues("Fixes #7 and also closes #7") == [7]

    def test_no_matches(self) -> None:
        assert parse_linked_issues("Just some description text") == []

    def test_empty(self) -> None:
        assert parse_linked_issues("") == []


class TestOnPRMerged:
    def test_closes_open_linked_issue(self) -> None:
        pr = make_pr(number=100)
        pr.body = "Fixes #42"
        tracked = {42: TrackedIssue(number=42, state=IssueTrackingState.IN_PROGRESS)}
        actions = on_pr_merged(pr, tracked)
        assert len(actions) == 1
        assert actions[0].kind is CascadeKind.CLOSE_ISSUE
        assert actions[0].target == 42
        assert actions[0].source == 100

    def test_skips_already_closed(self) -> None:
        pr = make_pr(number=100)
        pr.body = "Fixes #42"
        tracked = {42: TrackedIssue(number=42, state=IssueTrackingState.CLOSED)}
        assert on_pr_merged(pr, tracked) == []

    def test_skips_untracked(self) -> None:
        pr = make_pr(number=100)
        pr.body = "Fixes #99"
        assert on_pr_merged(pr, {}) == []


class TestOnPRClosedUnmerged:
    def test_unlinks_assigned_issue(self) -> None:
        pr = make_pr(number=100)
        tracked = {7: TrackedIssue(number=7, assigned_pr=100)}
        actions = on_pr_closed_unmerged(pr, tracked)
        assert len(actions) == 1
        assert actions[0].kind is CascadeKind.UNLINK_ISSUE
        assert actions[0].target == 7

    def test_ignores_unrelated_issues(self) -> None:
        pr = make_pr(number=100)
        tracked = {7: TrackedIssue(number=7, assigned_pr=999)}
        assert on_pr_closed_unmerged(pr, tracked) == []


class TestOnIssueClosedAsDuplicate:
    def _issue(self, number: int) -> Issue:
        return Issue(
            number=number,
            title=f"issue {number}",
            body="",
            state="closed",
            user=User(login="bot", id=1, type="Bot"),
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
        )

    def test_comments_on_linked_pr(self) -> None:
        issue = self._issue(5)
        tracked_prs = {200: TrackedPR(number=200)}
        bodies = {200: "Fixes #5\n\nThis PR has a real implementation " + "x" * 300}
        actions = on_issue_closed_as_duplicate(issue, 1, tracked_prs, bodies)
        kinds = [a.kind for a in actions]
        assert CascadeKind.COMMENT_ON_PR in kinds
        # Long body → no close action.
        assert CascadeKind.CLOSE_PR not in kinds

    def test_closes_pointless_pr(self) -> None:
        issue = self._issue(5)
        tracked_prs = {200: TrackedPR(number=200)}
        bodies = {200: "Fixes #5"}
        actions = on_issue_closed_as_duplicate(issue, 1, tracked_prs, bodies)
        kinds = [a.kind for a in actions]
        assert CascadeKind.COMMENT_ON_PR in kinds
        assert CascadeKind.CLOSE_PR in kinds

    def test_no_match(self) -> None:
        issue = self._issue(5)
        tracked_prs = {200: TrackedPR(number=200)}
        bodies = {200: "Unrelated PR"}
        assert on_issue_closed_as_duplicate(issue, 1, tracked_prs, bodies) == []


class _FakeGH:
    def __init__(self) -> None:
        self.comments: list[tuple[int, str]] = []
        self.closed: list[int] = []

    async def add_issue_comment(self, owner: str, repo: str, number: int, body: str) -> None:
        self.comments.append((number, body))

    async def update_issue(self, owner: str, repo: str, number: int, **kw: object) -> None:
        if kw.get("state") == "closed":
            self.closed.append(number)


@pytest.mark.asyncio
async def test_apply_cascade_close_issue_updates_tracked() -> None:
    gh = _FakeGH()
    tracked = {42: TrackedIssue(number=42, state=IssueTrackingState.IN_PROGRESS)}
    actions = [
        CascadeAction(
            kind=CascadeKind.CLOSE_ISSUE,
            target=42,
            reason="resolved",
            source=100,
        )
    ]
    report = await apply_cascade(gh, "o", "r", actions, tracked)
    assert 42 in gh.closed
    assert tracked[42].state is IssueTrackingState.COMPLETED
    assert len(report.applied) == 1


@pytest.mark.asyncio
async def test_apply_cascade_unlink_clears_assigned_pr() -> None:
    gh = _FakeGH()
    tracked = {7: TrackedIssue(number=7, assigned_pr=100)}
    actions = [CascadeAction(kind=CascadeKind.UNLINK_ISSUE, target=7, reason="abandoned")]
    await apply_cascade(gh, "o", "r", actions, tracked)
    assert tracked[7].assigned_pr is None
    assert tracked[7].state is IssueTrackingState.NEW


@pytest.mark.asyncio
async def test_apply_cascade_dry_run_no_side_effects() -> None:
    gh = _FakeGH()
    tracked = {42: TrackedIssue(number=42)}
    actions = [CascadeAction(kind=CascadeKind.CLOSE_ISSUE, target=42, reason="x")]
    report = await apply_cascade(gh, "o", "r", actions, tracked, dry_run=True)
    assert gh.closed == []
    assert len(report.skipped) == 1
