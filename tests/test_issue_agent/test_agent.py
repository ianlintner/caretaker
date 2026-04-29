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
        user=User(login="copilot-swe-agent[bot]", id=1, type="Bot"),
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

    async def test_assigned_issue_is_not_re_dispatched(self) -> None:
        """Issues already in ASSIGNED/IN_PROGRESS state must not be dispatched again."""
        github = AsyncMock()
        issue = make_issue(
            4,
            "Fix the bug",
            "there is a bug",
            assignees=[User(login="copilot-swe-agent[bot]", id=22, type="Bot")],
        )
        github.list_issues.return_value = [issue]
        github.list_pull_requests.return_value = []

        agent = IssueAgent(
            github=github,
            owner="o",
            repo="r",
            config=IssueAgentConfig(auto_assign_bugs=True),
        )

        for initial_state in (
            IssueTrackingState.ASSIGNED,
            IssueTrackingState.IN_PROGRESS,
            IssueTrackingState.ESCALATED,
        ):
            github.reset_mock()
            github.list_issues.return_value = [issue]
            github.list_pull_requests.return_value = []
            _report, tracked = await agent.run({4: TrackedIssue(number=4, state=initial_state)})
            # PR list is fetched once at the start of each run
            github.list_pull_requests.assert_awaited_once()
            # No comment or update should have been posted
            github.add_issue_comment.assert_not_awaited()
            github.update_issue.assert_not_awaited()
            github.create_issue.assert_not_awaited()
            # State should remain unchanged
            assert tracked[4].state == initial_state

    async def test_assigned_issue_resets_to_triaged_when_copilot_unassigned(self) -> None:
        """If Copilot is removed from assignees and no linked PR exists,
        an ASSIGNED/IN_PROGRESS issue must downgrade to TRIAGED for re-dispatch."""
        github = AsyncMock()
        # Issue is open but Copilot is NOT in assignees anymore
        issue = make_issue(5, "Re-open me", "copilot was unassigned")
        github.list_issues.return_value = [issue]
        github.list_pull_requests.return_value = []

        agent = IssueAgent(
            github=github,
            owner="o",
            repo="r",
            config=IssueAgentConfig(auto_assign_bugs=True),
        )

        for initial_state in (IssueTrackingState.ASSIGNED, IssueTrackingState.IN_PROGRESS):
            _report, tracked = await agent.run({5: TrackedIssue(number=5, state=initial_state)})
            assert tracked[5].state == IssueTrackingState.TRIAGED, (
                f"Expected TRIAGED when Copilot unassigned (was {initial_state}), "
                f"got {tracked[5].state}"
            )

    async def test_linked_pr_sets_pr_opened(self) -> None:
        github = AsyncMock()
        issue = make_issue(
            3,
            "Feature request",
            "please add a small feature",
            assignees=[User(login="copilot-swe-agent[bot]", id=22, type="Bot")],
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

    async def test_infra_escalation_comment_includes_debug_dump(self) -> None:
        github = AsyncMock()
        issue = make_issue(8, "Deploy pipeline issue", "workflow token missing in deploy step")
        github.list_issues.return_value = [issue]
        github.list_pull_requests.return_value = []

        agent = IssueAgent(
            github=github,
            owner="o",
            repo="r",
            config=IssueAgentConfig(auto_assign_features=False),
        )

        report, tracked = await agent.run({})

        assert 8 in report.escalated
        assert tracked[8].state == IssueTrackingState.ESCALATED
        # Escalation comments are upserted by marker (Sprint 2 A3) — body is
        # the 5th positional arg (owner, repo, number, marker, body).
        github.upsert_issue_comment.assert_awaited()
        comment_body = github.upsert_issue_comment.await_args.args[4]
        assert "Escalation debug dump" in comment_body
        assert '"type": "issue_escalation"' in comment_body
        assert '"classification": "INFRA_OR_CONFIG"' in comment_body
        assert "<!-- caretaker:escalation -->" in comment_body


@pytest.mark.asyncio
class TestTriageGate:
    """Two-phase triage gate: NEW → TRIAGED on first cycle, dispatch on second."""

    def _make_agent(self, github, **config_kwargs):
        return IssueAgent(
            github=github,
            owner="o",
            repo="r",
            config=IssueAgentConfig(triage_gate=True, **config_kwargs),
        )

    async def test_new_bug_parks_as_triaged_not_dispatched(self) -> None:
        """With triage_gate=True, a NEW bug is parked in TRIAGED — not dispatched immediately."""
        github = AsyncMock()
        issue = make_issue(10, "Button crashes on click", "clicking the submit button crashes")
        github.list_issues.return_value = [issue]
        github.list_pull_requests.return_value = []

        agent = self._make_agent(github, auto_assign_bugs=True)
        report, tracked = await agent.run({})

        assert tracked[10].state == IssueTrackingState.TRIAGED
        assert 10 not in report.assigned
        # triage-summary comment posted
        github.upsert_issue_comment.assert_awaited()
        comment_body = github.upsert_issue_comment.await_args.args[4]
        assert "<!-- caretaker:triage-summary -->" in comment_body
        assert "Caretaker Triage" in comment_body
        # caretaker:triaged label applied
        github.add_labels.assert_awaited()
        label_call_args = [str(call) for call in github.add_labels.await_args_list]
        assert any("caretaker:triaged" in s for s in label_call_args)

    async def test_triaged_bug_gets_dispatched_on_second_cycle(self) -> None:
        """A TRIAGED bug is dispatched on the next agent cycle (triage_gate=True)."""
        github = AsyncMock()
        issue = make_issue(11, "Button crashes on click", "clicking the submit button crashes")
        github.list_issues.return_value = [issue]
        github.list_pull_requests.return_value = []

        agent = self._make_agent(github, auto_assign_bugs=True)
        # Pre-seed issue as already TRIAGED (simulating second cycle)
        pre = {11: TrackedIssue(number=11, state=IssueTrackingState.TRIAGED)}
        report, tracked = await agent.run(pre)

        assert 11 in report.assigned
        assert tracked[11].state == IssueTrackingState.ASSIGNED

    async def test_new_duplicate_closes_immediately(self) -> None:
        """DUPLICATE issues are closed on the first cycle even when triage_gate=True."""
        github = AsyncMock()
        issue = make_issue(12, "Same bug again", "duplicate of #1")
        issue.labels = []
        github.list_issues.return_value = [issue]
        github.list_pull_requests.return_value = []

        agent = self._make_agent(github)
        report, tracked = await agent.run({})

        assert tracked[12].state == IssueTrackingState.CLOSED
        assert 12 in report.closed
        github.update_issue.assert_awaited()

    async def test_new_stale_closes_immediately(self) -> None:
        """STALE issues are closed on the first cycle even when triage_gate=True."""
        from datetime import timedelta

        github = AsyncMock()
        old = datetime.now(UTC) - timedelta(days=40)
        issue = make_issue(13, "Old forgotten bug", "still broken", updated_at=old)
        github.list_issues.return_value = [issue]
        github.list_pull_requests.return_value = []

        agent = self._make_agent(github, auto_close_stale_days=30)
        report, tracked = await agent.run({})

        assert tracked[13].state == IssueTrackingState.STALE
        assert 13 in report.closed

    async def test_triage_gate_false_dispatches_immediately(self) -> None:
        """With triage_gate=False (default), bugs are dispatched immediately — legacy behavior."""
        github = AsyncMock()
        issue = make_issue(14, "Button crashes on click", "clicking the submit button crashes")
        github.list_issues.return_value = [issue]
        github.list_pull_requests.return_value = []

        agent = IssueAgent(
            github=github,
            owner="o",
            repo="r",
            config=IssueAgentConfig(triage_gate=False, auto_assign_bugs=True),
        )
        report, tracked = await agent.run({})

        assert 14 in report.assigned
        assert tracked[14].state == IssueTrackingState.ASSIGNED
