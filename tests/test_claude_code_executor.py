"""Tests for the Claude Code hand-off executor + dispatcher wiring."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from caretaker.claude_code_executor import (
    CLAUDE_CODE_HANDOFF_MARKER,
    ClaudeCodeExecutor,
)
from caretaker.config import (
    ClaudeCodeExecutorConfig,
    ExecutorConfig,
    FoundryExecutorConfig,
)
from caretaker.foundry.dispatcher import (
    LABEL_AGENT_CUSTOM,
    ExecutorDispatcher,
    RouteOutcome,
)
from caretaker.foundry.executor import (
    CodingTask,
    ExecutorOutcome,
    ExecutorResult,
)
from caretaker.github_client.models import Comment, Label, PullRequest, User
from caretaker.llm.copilot import CopilotTask, TaskType

# ── Fixtures ──────────────────────────────────────────────────────────────


def _coding_task(task_type: TaskType = TaskType.LINT_FAILURE) -> CodingTask:
    return CodingTask(
        task_type=task_type,
        job_name="lint",
        error_output="E501",
        instructions="fix line length",
    )


def _copilot_task(task_type: TaskType = TaskType.LINT_FAILURE) -> CopilotTask:
    return CopilotTask(
        task_type=task_type,
        job_name="lint",
        error_output="E501",
        instructions="fix line length",
        attempt=1,
        max_attempts=2,
    )


def _pr(number: int = 42, labels: list[str] | None = None) -> PullRequest:
    return PullRequest(
        number=number,
        title="test",
        body="",
        state="open",
        user=User(login="dev", id=1),
        head_ref="feat",
        head_sha="abc",
        base_ref="main",
        labels=[Label(name=n) for n in (labels or [])],
    )


def _comment(cid: int = 1) -> Comment:
    return Comment(
        id=cid,
        user=User(login="caretaker-bot", id=99, type="Bot"),
        body=CLAUDE_CODE_HANDOFF_MARKER,
        created_at=datetime(2026, 4, 21, tzinfo=UTC),
    )


# ── Config ────────────────────────────────────────────────────────────────


def test_config_defaults_disabled() -> None:
    cfg = ExecutorConfig()
    assert cfg.claude_code.enabled is False
    assert cfg.claude_code.trigger_label == "claude-code"
    assert cfg.claude_code.mention == "@claude"
    assert cfg.claude_code.max_attempts == 2


def test_provider_literal_accepts_claude_code() -> None:
    cfg = ExecutorConfig(provider="claude_code")
    assert cfg.provider == "claude_code"


# ── Executor behaviour ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_executor_disabled_escalates() -> None:
    github = MagicMock()
    github.add_issue_comment = AsyncMock()
    github.add_labels = AsyncMock()
    github.get_pr_comments = AsyncMock(return_value=[])
    executor = ClaudeCodeExecutor(
        github=github,
        owner="o",
        repo="r",
        config=ClaudeCodeExecutorConfig(enabled=False),
    )
    result = await executor.run(_coding_task(), _pr())
    assert result.outcome == ExecutorOutcome.ESCALATED
    github.add_issue_comment.assert_not_called()
    github.add_labels.assert_not_called()


@pytest.mark.asyncio
async def test_executor_posts_comment_and_applies_label() -> None:
    github = MagicMock()
    github.add_issue_comment = AsyncMock(return_value=_comment(cid=777))
    github.add_labels = AsyncMock(return_value=[Label(name="claude-code")])
    github.get_pr_comments = AsyncMock(return_value=[])
    executor = ClaudeCodeExecutor(
        github=github,
        owner="o",
        repo="r",
        config=ClaudeCodeExecutorConfig(enabled=True),
    )
    result = await executor.run(_coding_task(), _pr(42))
    assert result.outcome == ExecutorOutcome.COMPLETED
    assert result.comment_id == 777
    github.add_issue_comment.assert_awaited_once()
    args = github.add_issue_comment.await_args.args
    assert args[:3] == ("o", "r", 42)
    body = args[3]
    assert CLAUDE_CODE_HANDOFF_MARKER in body
    assert "@claude" in body
    assert "LINT_FAILURE" in body
    github.add_labels.assert_awaited_once_with("o", "r", 42, ["claude-code"])


@pytest.mark.asyncio
async def test_executor_label_failure_still_completes_via_mention() -> None:
    github = MagicMock()
    github.add_issue_comment = AsyncMock(return_value=_comment())
    github.add_labels = AsyncMock(side_effect=RuntimeError("permissions"))
    github.get_pr_comments = AsyncMock(return_value=[])
    executor = ClaudeCodeExecutor(
        github=github,
        owner="o",
        repo="r",
        config=ClaudeCodeExecutorConfig(enabled=True),
    )
    result = await executor.run(_coding_task(), _pr())
    assert result.outcome == ExecutorOutcome.COMPLETED
    assert "label apply failed" in result.reason


@pytest.mark.asyncio
async def test_executor_comment_failure_fails() -> None:
    github = MagicMock()
    github.add_issue_comment = AsyncMock(side_effect=RuntimeError("api down"))
    github.add_labels = AsyncMock()
    github.get_pr_comments = AsyncMock(return_value=[])
    executor = ClaudeCodeExecutor(
        github=github,
        owner="o",
        repo="r",
        config=ClaudeCodeExecutorConfig(enabled=True),
    )
    result = await executor.run(_coding_task(), _pr())
    assert result.outcome == ExecutorOutcome.FAILED
    github.add_labels.assert_not_called()


@pytest.mark.asyncio
async def test_attempt_cap_escalates() -> None:
    # Two prior hand-offs on the PR; max_attempts=2 → this call should escalate.
    prior = [_comment(cid=1), _comment(cid=2)]
    github = MagicMock()
    github.get_pr_comments = AsyncMock(return_value=prior)
    github.add_issue_comment = AsyncMock()
    github.add_labels = AsyncMock()
    executor = ClaudeCodeExecutor(
        github=github,
        owner="o",
        repo="r",
        config=ClaudeCodeExecutorConfig(enabled=True, max_attempts=2),
    )
    result = await executor.run(_coding_task(), _pr())
    assert result.outcome == ExecutorOutcome.ESCALATED
    assert "cap hit" in result.reason
    github.add_issue_comment.assert_not_called()


# ── Dispatcher routing ────────────────────────────────────────────────────


def _dispatcher(
    provider: str = "claude_code",
    claude_code_enabled: bool = True,
    foundry_enabled: bool = False,
    executor: ClaudeCodeExecutor | None = None,
) -> tuple[ExecutorDispatcher, MagicMock, MagicMock]:
    cfg = ExecutorConfig(
        provider=provider,  # type: ignore[arg-type]
        foundry=FoundryExecutorConfig(enabled=foundry_enabled),
        claude_code=ClaudeCodeExecutorConfig(enabled=claude_code_enabled),
    )
    copilot = MagicMock()
    copilot.post_task = AsyncMock(return_value=_comment())
    if executor is None:
        executor = MagicMock()
        executor.run = AsyncMock(
            return_value=ExecutorResult(
                outcome=ExecutorOutcome.COMPLETED,
                reason="dispatched",
                comment_id=555,
            )
        )
        # ClaudeCodeExecutor accesses .config via @property on the real
        # class; we don't touch it from the dispatcher so the mock is fine.
    foundry = None
    if foundry_enabled:
        foundry = MagicMock()
        foundry.run = AsyncMock(
            return_value=ExecutorResult(outcome=ExecutorOutcome.COMPLETED, reason="ok")
        )
    dispatcher = ExecutorDispatcher(
        config=cfg,
        foundry_executor=foundry,
        copilot_protocol=copilot,
        claude_code_executor=executor,
    )
    return dispatcher, copilot, executor


@pytest.mark.asyncio
async def test_dispatcher_claude_code_provider_routes_to_claude() -> None:
    dispatcher, copilot, executor = _dispatcher()
    route = await dispatcher.route(pr=_pr(), copilot_task=_copilot_task())
    assert route.outcome == RouteOutcome.CLAUDE_CODE
    executor.run.assert_awaited_once()
    copilot.post_task.assert_not_called()


@pytest.mark.asyncio
async def test_dispatcher_claude_code_misconfig_falls_to_copilot() -> None:
    # provider says claude_code but feature is disabled → Copilot.
    dispatcher, copilot, executor = _dispatcher(claude_code_enabled=False)
    route = await dispatcher.route(pr=_pr(), copilot_task=_copilot_task())
    assert route.outcome == RouteOutcome.COPILOT
    copilot.post_task.assert_awaited_once()
    executor.run.assert_not_called()


@pytest.mark.asyncio
async def test_dispatcher_claude_code_escalation_falls_back() -> None:
    executor = MagicMock()
    executor.run = AsyncMock(
        return_value=ExecutorResult(
            outcome=ExecutorOutcome.ESCALATED,
            reason="attempt cap hit",
        )
    )
    dispatcher, copilot, _ = _dispatcher(executor=executor)
    route = await dispatcher.route(pr=_pr(), copilot_task=_copilot_task())
    assert route.outcome == RouteOutcome.COPILOT_FALLBACK
    copilot.post_task.assert_awaited_once()
    # The Copilot fallback task should carry the escalation context.
    posted = copilot.post_task.await_args.args[1]
    assert "attempted this task and escalated" in posted.context


@pytest.mark.asyncio
async def test_agent_custom_label_prefers_claude_when_it_is_the_provider() -> None:
    # Foundry also enabled; provider=claude_code → claude wins.
    dispatcher, copilot, executor = _dispatcher(foundry_enabled=True)
    route = await dispatcher.route(
        pr=_pr(labels=[LABEL_AGENT_CUSTOM]),
        copilot_task=_copilot_task(),
    )
    assert route.outcome == RouteOutcome.CLAUDE_CODE
    executor.run.assert_awaited_once()


@pytest.mark.asyncio
async def test_auto_provider_falls_to_claude_when_foundry_ineligible() -> None:
    # provider=auto, foundry restricted to a task type our task doesn't match,
    # claude_code enabled → claude picks it up before Copilot.
    cfg = ExecutorConfig(
        provider="auto",
        foundry=FoundryExecutorConfig(enabled=True, allowed_task_types=["UPGRADE"]),
        claude_code=ClaudeCodeExecutorConfig(enabled=True),
    )
    copilot = MagicMock()
    copilot.post_task = AsyncMock(return_value=_comment())
    foundry = MagicMock()
    foundry.run = AsyncMock()
    claude = MagicMock()
    claude.run = AsyncMock(
        return_value=ExecutorResult(outcome=ExecutorOutcome.COMPLETED, reason="dispatched")
    )
    dispatcher = ExecutorDispatcher(
        config=cfg,
        foundry_executor=foundry,
        copilot_protocol=copilot,
        claude_code_executor=claude,
    )
    route = await dispatcher.route(pr=_pr(), copilot_task=_copilot_task(TaskType.LINT_FAILURE))
    assert route.outcome == RouteOutcome.CLAUDE_CODE
    foundry.run.assert_not_called()
    claude.run.assert_awaited_once()
    copilot.post_task.assert_not_called()
