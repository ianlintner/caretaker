"""Tests for the BYOCA hand-off agents (Claude Code + opencode)."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from caretaker.coding_agents.handoff import (
    CLAUDE_CODE_HANDOFF_MARKER,
    OPENCODE_HANDOFF_MARKER,
    ClaudeCodeAgent,
    OpenCodeAgent,
)
from caretaker.config import (
    ClaudeCodeExecutorConfig,
    OpenCodeExecutorConfig,
)
from caretaker.foundry.executor import (
    CodingTask,
    ExecutorOutcome,
)
from caretaker.github_client.models import Comment, Label, PullRequest, User
from caretaker.llm.copilot import TaskType


def _coding_task() -> CodingTask:
    return CodingTask(
        task_type=TaskType.LINT_FAILURE,
        job_name="lint",
        error_output="E501",
        instructions="fix line length",
    )


def _pr() -> PullRequest:
    return PullRequest(
        number=42,
        title="test",
        body="",
        state="open",
        user=User(login="dev", id=1),
        head_ref="feat",
        head_sha="abc",
        base_ref="main",
        labels=[],
    )


def _comment(marker: str, cid: int = 1) -> Comment:
    return Comment(
        id=cid,
        user=User(login="caretaker-bot", id=99, type="Bot"),
        body=marker,
        created_at=datetime(2026, 4, 21, tzinfo=UTC),
    )


# Markers must be unique per agent so attempt-counts don't cross-contaminate.
def test_markers_distinct() -> None:
    assert CLAUDE_CODE_HANDOFF_MARKER != OPENCODE_HANDOFF_MARKER


# ── OpenCodeAgent ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_opencode_posts_comment_and_applies_label() -> None:
    github = MagicMock()
    github.add_issue_comment = AsyncMock(return_value=_comment(OPENCODE_HANDOFF_MARKER, cid=777))
    github.add_labels = AsyncMock(return_value=[Label(name="opencode")])
    github.get_pr_comments = AsyncMock(return_value=[])
    agent = OpenCodeAgent(
        github=github,
        owner="o",
        repo="r",
        config=OpenCodeExecutorConfig(enabled=True),
    )
    result = await agent.run(_coding_task(), _pr())
    assert result.outcome == ExecutorOutcome.COMPLETED
    assert result.comment_id == 777
    body = github.add_issue_comment.await_args.args[3]
    # The body must carry the opencode marker AND the opencode mention,
    # not the Claude marker — protects against accidental marker reuse.
    assert OPENCODE_HANDOFF_MARKER in body
    assert CLAUDE_CODE_HANDOFF_MARKER not in body
    assert "@opencode-agent" in body
    github.add_labels.assert_awaited_once_with("o", "r", 42, ["opencode"])


@pytest.mark.asyncio
async def test_opencode_disabled_escalates() -> None:
    github = MagicMock()
    github.get_pr_comments = AsyncMock(return_value=[])
    github.add_issue_comment = AsyncMock()
    github.add_labels = AsyncMock()
    agent = OpenCodeAgent(
        github=github, owner="o", repo="r", config=OpenCodeExecutorConfig(enabled=False)
    )
    result = await agent.run(_coding_task(), _pr())
    assert result.outcome == ExecutorOutcome.ESCALATED
    github.add_issue_comment.assert_not_called()


@pytest.mark.asyncio
async def test_opencode_attempt_cap_uses_own_marker() -> None:
    # Claude-code prior comments must NOT count toward opencode's cap.
    prior_claude = [
        _comment(CLAUDE_CODE_HANDOFF_MARKER, cid=1),
        _comment(CLAUDE_CODE_HANDOFF_MARKER, cid=2),
    ]
    prior_opencode = [_comment(OPENCODE_HANDOFF_MARKER, cid=3)]
    github = MagicMock()
    github.get_pr_comments = AsyncMock(return_value=prior_claude + prior_opencode)
    github.add_issue_comment = AsyncMock(return_value=_comment(OPENCODE_HANDOFF_MARKER, cid=99))
    github.add_labels = AsyncMock(return_value=[])
    agent = OpenCodeAgent(
        github=github,
        owner="o",
        repo="r",
        config=OpenCodeExecutorConfig(enabled=True, max_attempts=2),
    )
    # Only 1 prior opencode hand-off → attempt 2 → still under cap.
    result = await agent.run(_coding_task(), _pr())
    assert result.outcome == ExecutorOutcome.COMPLETED
    assert result.iterations == 2


# ── ClaudeCodeAgent — backward-compat sanity ─────────────────────────────


@pytest.mark.asyncio
async def test_claude_code_uses_legacy_marker() -> None:
    github = MagicMock()
    github.add_issue_comment = AsyncMock(return_value=_comment(CLAUDE_CODE_HANDOFF_MARKER, cid=11))
    github.add_labels = AsyncMock(return_value=[])
    github.get_pr_comments = AsyncMock(return_value=[])
    agent = ClaudeCodeAgent(
        github=github,
        owner="o",
        repo="r",
        config=ClaudeCodeExecutorConfig(enabled=True),
    )
    result = await agent.run(_coding_task(), _pr())
    assert result.outcome == ExecutorOutcome.COMPLETED
    body = github.add_issue_comment.await_args.args[3]
    assert CLAUDE_CODE_HANDOFF_MARKER in body
    assert OPENCODE_HANDOFF_MARKER not in body
    assert "@claude" in body


def test_claude_code_executor_legacy_alias() -> None:
    """``ClaudeCodeExecutor`` is a deprecation alias for ``ClaudeCodeAgent``."""
    from caretaker.claude_code_executor import ClaudeCodeAgent as Aliased
    from caretaker.claude_code_executor import ClaudeCodeExecutor

    assert ClaudeCodeExecutor is Aliased
