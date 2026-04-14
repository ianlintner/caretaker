"""Tests for repo-bound GitHub tool helpers."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from caretaker.tools.github import GitHubIssueTools


@pytest.mark.asyncio
async def test_issue_tools_default_assignment_targets_bound_repo() -> None:
    github = AsyncMock()
    issues = GitHubIssueTools(github, "octo", "widgets")

    assignment = issues.default_copilot_assignment(base_branch="main")

    assert assignment.to_api_payload() == {
        "target_repo": "octo/widgets",
        "base_branch": "main",
    }


@pytest.mark.asyncio
async def test_issue_tools_assign_copilot_forwards_assignment() -> None:
    github = AsyncMock()
    issues = GitHubIssueTools(github, "octo", "widgets")
    assignment = issues.default_copilot_assignment(custom_instructions="be nice")

    await issues.assign_copilot(42, assignment)

    github.assign_copilot_to_issue.assert_awaited_once_with(
        "octo",
        "widgets",
        42,
        assignment=assignment,
    )
