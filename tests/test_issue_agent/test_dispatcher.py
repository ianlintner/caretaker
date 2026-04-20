"""Tests for issue dispatcher."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from caretaker.github_client.models import Issue, User
from caretaker.issue_agent.classifier import IssueClassification
from caretaker.issue_agent.dispatcher import IssueDispatcher, build_assignment_body


def make_issue(number: int = 1, title: str = "Bug report") -> Issue:
    return Issue(
        number=number,
        title=title,
        body="something is broken",
        user=User(login="reporter", id=1, type="User"),
    )


@pytest.mark.asyncio
class TestIssueDispatcher:
    async def test_bug_simple_dispatches_in_place(self) -> None:
        github = AsyncMock()
        dispatcher = IssueDispatcher(github=github, owner="o", repo="r")
        issue = make_issue()

        result = await dispatcher.dispatch(issue, IssueClassification.BUG_SIMPLE)

        assert result == issue
        github.add_issue_comment.assert_awaited_once()
        github.update_issue.assert_awaited_once()

    async def test_feature_small_creates_assignment_issue(self) -> None:
        github = AsyncMock()
        dispatcher = IssueDispatcher(github=github, owner="o", repo="r")
        issue = make_issue(number=2, title="Add feature")
        github.create_issue.return_value = make_issue(number=99, title="[Maintainer] Add feature")

        result = await dispatcher.dispatch(issue, IssueClassification.FEATURE_SMALL)

        assert result is not None
        assert result.number == 99
        github.create_issue.assert_awaited_once()
        github.add_issue_comment.assert_awaited_once()

    async def test_non_dispatchable_returns_none(self) -> None:
        github = AsyncMock()
        dispatcher = IssueDispatcher(github=github, owner="o", repo="r")
        issue = make_issue()

        result = await dispatcher.dispatch(issue, IssueClassification.INFRA_OR_CONFIG)

        assert result is None
        github.create_issue.assert_not_called()
        github.update_issue.assert_not_called()

    async def test_bug_simple_emits_causal_marker(self) -> None:
        github = AsyncMock()
        dispatcher = IssueDispatcher(github=github, owner="o", repo="r")
        issue = make_issue()

        await dispatcher.dispatch(issue, IssueClassification.BUG_SIMPLE)

        comment_body = github.add_issue_comment.call_args.args[3]
        assert "caretaker:causal" in comment_body
        assert "source=issue-agent:dispatch" in comment_body

    async def test_bug_simple_inherits_parent_causal_from_issue_body(self) -> None:
        github = AsyncMock()
        dispatcher = IssueDispatcher(github=github, owner="o", repo="r")
        issue = Issue(
            number=7,
            title="Broken",
            body="<!-- caretaker:causal id=run-42-devops source=devops -->\ndetail",
            user=User(login="reporter", id=1, type="User"),
        )

        await dispatcher.dispatch(issue, IssueClassification.BUG_SIMPLE)

        comment_body = github.add_issue_comment.call_args.args[3]
        assert "parent=run-42-devops" in comment_body

    async def test_build_assignment_body_emits_causal_marker(self) -> None:
        issue = make_issue()
        body = build_assignment_body(issue, IssueClassification.FEATURE_SMALL)
        assert "caretaker:causal" in body
        assert "source=issue-agent:dispatch" in body

    async def test_bug_simple_assigns_copilot_via_update_issue(self) -> None:
        """Dispatcher forwards both the logical Copilot assignee and repo routing metadata."""
        github = AsyncMock()
        github.update_issue.return_value = make_issue()
        dispatcher = IssueDispatcher(github=github, owner="o", repo="r")
        issue = make_issue()

        await dispatcher.dispatch(issue, IssueClassification.BUG_SIMPLE)

        github.update_issue.assert_awaited_once()
        call_kwargs = github.update_issue.call_args.kwargs
        assert "copilot" in call_kwargs.get("assignees", [])
        assignment = call_kwargs.get("copilot_assignment")
        assert assignment is not None
        assert assignment.to_api_payload()["target_repo"] == "o/r"
