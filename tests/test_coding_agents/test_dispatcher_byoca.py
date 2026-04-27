"""BYOCA-specific dispatcher tests.

The classic dispatcher tests live in ``tests/test_foundry/test_dispatcher.py``
and ``tests/test_claude_code_executor.py`` (which now exercise the
registry path through the back-compat shim). These tests cover the
new opencode-aware behaviour and the generic ``agent:<name>`` label
override.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from caretaker.coding_agents.handoff import OpenCodeAgent
from caretaker.coding_agents.registry import CodingAgentRegistry
from caretaker.config import (
    ExecutorConfig,
    FoundryExecutorConfig,
    OpenCodeExecutorConfig,
)
from caretaker.foundry.dispatcher import ExecutorDispatcher, RouteOutcome
from caretaker.foundry.executor import ExecutorOutcome, ExecutorResult
from caretaker.github_client.models import Comment, Label, PullRequest, User
from caretaker.llm.copilot import CopilotTask, TaskType


def _pr(labels: list[str] | None = None) -> PullRequest:
    return PullRequest(
        number=42,
        title="t",
        body="",
        state="open",
        user=User(login="dev", id=1),
        head_ref="feat",
        head_sha="abc",
        base_ref="main",
        labels=[Label(name=n) for n in (labels or [])],
    )


def _copilot_task() -> CopilotTask:
    return CopilotTask(
        task_type=TaskType.LINT_FAILURE,
        job_name="lint",
        error_output="E501",
        instructions="fix",
        attempt=1,
        max_attempts=2,
    )


def _comment() -> Comment:
    from datetime import UTC, datetime

    return Comment(
        id=1,
        user=User(login="bot", id=2, type="Bot"),
        body="",
        created_at=datetime(2026, 4, 21, tzinfo=UTC),
    )


def _build(
    *,
    provider: str,
    opencode_enabled: bool = True,
    foundry_enabled: bool = False,
) -> tuple[ExecutorDispatcher, MagicMock, MagicMock]:
    cfg = ExecutorConfig(
        provider=provider,
        foundry=FoundryExecutorConfig(enabled=foundry_enabled),
        opencode=OpenCodeExecutorConfig(enabled=opencode_enabled),
    )
    copilot = MagicMock()
    copilot.post_task = AsyncMock(return_value=_comment())
    registry = CodingAgentRegistry()
    opencode_agent = MagicMock(spec=OpenCodeAgent)
    opencode_agent.name = "opencode"
    opencode_agent.enabled = opencode_enabled
    opencode_agent.run = AsyncMock(
        return_value=ExecutorResult(
            outcome=ExecutorOutcome.COMPLETED, reason="dispatched", comment_id=1
        )
    )
    if opencode_enabled:
        registry.register(opencode_agent)
    foundry = None
    dispatcher = ExecutorDispatcher(
        config=cfg,
        foundry_executor=foundry,
        copilot_protocol=copilot,
        registry=registry,
    )
    return dispatcher, copilot, opencode_agent


@pytest.mark.asyncio
async def test_provider_opencode_routes_to_opencode_agent() -> None:
    dispatcher, copilot, agent = _build(provider="opencode")
    route = await dispatcher.route(pr=_pr(), copilot_task=_copilot_task())
    assert route.outcome == RouteOutcome.CUSTOM_AGENT
    assert route.agent_name == "opencode"
    agent.run.assert_awaited_once()
    copilot.post_task.assert_not_called()


@pytest.mark.asyncio
async def test_label_override_agent_opencode() -> None:
    # provider=copilot but `agent:opencode` label forces the registered
    # opencode agent.
    dispatcher, copilot, agent = _build(provider="copilot")
    route = await dispatcher.route(
        pr=_pr(labels=["agent:opencode"]),
        copilot_task=_copilot_task(),
    )
    assert route.outcome == RouteOutcome.CUSTOM_AGENT
    assert route.agent_name == "opencode"
    agent.run.assert_awaited_once()
    copilot.post_task.assert_not_called()


@pytest.mark.asyncio
async def test_unknown_agent_label_falls_back_to_copilot() -> None:
    # `agent:gemini` on a PR but gemini isn't registered → Copilot fallback,
    # not a crash.
    dispatcher, copilot, agent = _build(provider="copilot")
    route = await dispatcher.route(
        pr=_pr(labels=["agent:gemini"]),
        copilot_task=_copilot_task(),
    )
    assert route.outcome == RouteOutcome.COPILOT_FALLBACK
    copilot.post_task.assert_awaited_once()
    agent.run.assert_not_called()


@pytest.mark.asyncio
async def test_unknown_provider_logs_and_routes_to_copilot() -> None:
    dispatcher, copilot, agent = _build(provider="hermes")
    route = await dispatcher.route(pr=_pr(), copilot_task=_copilot_task())
    assert route.outcome == RouteOutcome.COPILOT
    copilot.post_task.assert_awaited_once()
    agent.run.assert_not_called()


@pytest.mark.asyncio
async def test_opencode_disabled_falls_to_copilot_when_named() -> None:
    dispatcher, copilot, agent = _build(provider="opencode", opencode_enabled=False)
    route = await dispatcher.route(pr=_pr(), copilot_task=_copilot_task())
    assert route.outcome == RouteOutcome.COPILOT
    copilot.post_task.assert_awaited_once()
    agent.run.assert_not_called()
