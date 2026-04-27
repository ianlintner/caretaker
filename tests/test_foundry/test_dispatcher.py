"""Tests for ExecutorDispatcher routing decisions."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from caretaker.config import ExecutorConfig, FoundryExecutorConfig
from caretaker.foundry.dispatcher import (
    LABEL_AGENT_COPILOT,
    LABEL_AGENT_CUSTOM,
    LABEL_AGENT_QUARANTINE,
    ExecutorDispatcher,
    RouteOutcome,
    routing_override,
)
from caretaker.foundry.executor import (
    CodingTask,
    ExecutorOutcome,
    ExecutorResult,
)
from caretaker.github_client.models import Comment, Label, PullRequest, User
from caretaker.llm.copilot import CopilotTask, TaskType


def _make_pr(number: int = 42, labels: list[str] | None = None) -> PullRequest:
    return PullRequest(
        number=number,
        title="test",
        body="",
        state="open",
        user=User(login="dev", id=1),
        head_ref="feat",
        head_sha="abc123",
        base_ref="main",
        labels=[Label(name=n) for n in (labels or [])],
    )


def _make_copilot_task(
    task_type: TaskType = TaskType.LINT_FAILURE,
) -> CopilotTask:
    return CopilotTask(
        task_type=task_type,
        job_name="lint",
        error_output="E501",
        instructions="fix it",
        attempt=1,
        max_attempts=2,
    )


def _make_comment() -> Comment:
    return Comment(
        id=555,
        user=User(login="caretaker-bot", id=99, type="Bot"),
        body="<!-- caretaker:task -->\n@copilot fix\n<!-- /caretaker:task -->",
        created_at=datetime(2024, 1, 1, tzinfo=UTC),
    )


class TestDispatcherCopilotDefault:
    @pytest.mark.asyncio
    async def test_provider_copilot_always_posts_copilot(self) -> None:
        cfg = ExecutorConfig(provider="copilot")
        protocol = MagicMock()
        protocol.post_task = AsyncMock(return_value=_make_comment())
        dispatcher = ExecutorDispatcher(
            config=cfg, foundry_executor=None, copilot_protocol=protocol
        )
        route = await dispatcher.route(pr=_make_pr(), copilot_task=_make_copilot_task())
        assert route.outcome == RouteOutcome.COPILOT
        protocol.post_task.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_executor_routes_to_copilot(self) -> None:
        cfg = ExecutorConfig(provider="auto")
        protocol = MagicMock()
        protocol.post_task = AsyncMock(return_value=_make_comment())
        dispatcher = ExecutorDispatcher(
            config=cfg, foundry_executor=None, copilot_protocol=protocol
        )
        route = await dispatcher.route(pr=_make_pr(), copilot_task=_make_copilot_task())
        assert route.outcome == RouteOutcome.COPILOT


class TestDispatcherFoundryRouting:
    @pytest.mark.asyncio
    async def test_foundry_success_routes_foundry(self) -> None:
        cfg = ExecutorConfig(
            provider="foundry",
            foundry=FoundryExecutorConfig(enabled=True),
        )
        protocol = MagicMock()
        protocol.post_task = AsyncMock(return_value=_make_comment())
        foundry = MagicMock()
        foundry.run = AsyncMock(
            return_value=ExecutorResult(
                outcome=ExecutorOutcome.COMPLETED,
                reason="pushed",
                commit_sha="def456",
                comment_id=1234,
            )
        )
        dispatcher = ExecutorDispatcher(
            config=cfg, foundry_executor=foundry, copilot_protocol=protocol
        )
        route = await dispatcher.route(pr=_make_pr(), copilot_task=_make_copilot_task())
        assert route.outcome == RouteOutcome.FOUNDRY
        assert route.foundry_result is not None
        assert route.foundry_result.commit_sha == "def456"
        protocol.post_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_foundry_escalation_falls_back_to_copilot(self) -> None:
        cfg = ExecutorConfig(
            provider="foundry",
            foundry=FoundryExecutorConfig(enabled=True),
        )
        protocol = MagicMock()
        protocol.post_task = AsyncMock(return_value=_make_comment())
        foundry = MagicMock()
        foundry.run = AsyncMock(
            return_value=ExecutorResult(
                outcome=ExecutorOutcome.ESCALATED,
                reason="no changes produced",
            )
        )
        dispatcher = ExecutorDispatcher(
            config=cfg, foundry_executor=foundry, copilot_protocol=protocol
        )
        route = await dispatcher.route(pr=_make_pr(), copilot_task=_make_copilot_task())
        assert route.outcome == RouteOutcome.COPILOT_FALLBACK
        protocol.post_task.assert_awaited_once()
        posted_task = protocol.post_task.await_args.args[1]
        assert "caretaker-foundry attempted" in posted_task.context

    @pytest.mark.asyncio
    async def test_auto_ineligible_task_routes_copilot(self) -> None:
        cfg = ExecutorConfig(
            provider="auto",
            foundry=FoundryExecutorConfig(enabled=True, allowed_task_types=["UPGRADE"]),
        )
        protocol = MagicMock()
        protocol.post_task = AsyncMock(return_value=_make_comment())
        foundry = MagicMock()
        foundry.run = AsyncMock()
        dispatcher = ExecutorDispatcher(
            config=cfg, foundry_executor=foundry, copilot_protocol=protocol
        )
        # LINT_FAILURE not in allow-list → route to Copilot.
        route = await dispatcher.route(
            pr=_make_pr(), copilot_task=_make_copilot_task(TaskType.LINT_FAILURE)
        )
        assert route.outcome == RouteOutcome.COPILOT
        foundry.run.assert_not_called()


class TestToCodingTask:
    def test_lint_gets_preferred_command(self) -> None:
        task = _make_copilot_task(TaskType.LINT_FAILURE)
        coding = ExecutorDispatcher._to_coding_task(task)
        assert coding.preferred_command == ("ruff", ["check", "."])

    def test_non_lint_has_no_preferred_command(self) -> None:
        task = _make_copilot_task(TaskType.REVIEW_COMMENT)
        coding = ExecutorDispatcher._to_coding_task(task)
        assert coding.preferred_command is None


class TestFoundryEligibility:
    def test_requires_executor_present(self) -> None:
        cfg = ExecutorConfig(provider="auto", foundry=FoundryExecutorConfig(enabled=True))
        dispatcher = ExecutorDispatcher(
            config=cfg, foundry_executor=None, copilot_protocol=MagicMock()
        )
        task = CodingTask(
            task_type=TaskType.LINT_FAILURE,
            job_name="lint",
            error_output="x",
            instructions="fix",
        )
        assert dispatcher.foundry_eligible(task) is False


class TestRoutingOverride:
    def test_quarantine_wins_over_custom(self) -> None:
        labels = [Label(name=LABEL_AGENT_CUSTOM), Label(name=LABEL_AGENT_QUARANTINE)]
        assert routing_override(labels) == LABEL_AGENT_QUARANTINE

    def test_custom_wins_over_copilot(self) -> None:
        labels = [Label(name=LABEL_AGENT_COPILOT), Label(name=LABEL_AGENT_CUSTOM)]
        assert routing_override(labels) == LABEL_AGENT_CUSTOM

    def test_no_routing_label(self) -> None:
        labels = [Label(name="size/S"), Label(name="kind/bug")]
        assert routing_override(labels) is None

    def test_accepts_str_list(self) -> None:
        assert routing_override([LABEL_AGENT_CUSTOM]) == LABEL_AGENT_CUSTOM

    def test_accepts_dict_list(self) -> None:
        assert routing_override([{"name": LABEL_AGENT_COPILOT}]) == LABEL_AGENT_COPILOT

    def test_none_input(self) -> None:
        assert routing_override(None) is None


class TestLabelOverrideRouting:
    @pytest.mark.asyncio
    async def test_quarantine_label_refuses_dispatch(self) -> None:
        cfg = ExecutorConfig(provider="foundry", foundry=FoundryExecutorConfig(enabled=True))
        protocol = MagicMock()
        protocol.post_task = AsyncMock(return_value=_make_comment())
        foundry = MagicMock()
        foundry.run = AsyncMock()
        dispatcher = ExecutorDispatcher(
            config=cfg, foundry_executor=foundry, copilot_protocol=protocol
        )
        route = await dispatcher.route(
            pr=_make_pr(labels=[LABEL_AGENT_QUARANTINE]),
            copilot_task=_make_copilot_task(),
        )
        assert route.outcome == RouteOutcome.REFUSED
        foundry.run.assert_not_called()
        protocol.post_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_agent_custom_label_forces_foundry(self) -> None:
        # provider=copilot would normally skip Foundry entirely; the label
        # must override that.
        cfg = ExecutorConfig(provider="copilot", foundry=FoundryExecutorConfig(enabled=True))
        protocol = MagicMock()
        protocol.post_task = AsyncMock(return_value=_make_comment())
        foundry = MagicMock()
        foundry.run = AsyncMock(
            return_value=ExecutorResult(
                outcome=ExecutorOutcome.COMPLETED,
                reason="pushed",
                commit_sha="cafef00d",
            )
        )
        dispatcher = ExecutorDispatcher(
            config=cfg, foundry_executor=foundry, copilot_protocol=protocol
        )
        route = await dispatcher.route(
            pr=_make_pr(labels=[LABEL_AGENT_CUSTOM]),
            copilot_task=_make_copilot_task(),
        )
        assert route.outcome == RouteOutcome.FOUNDRY
        protocol.post_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_agent_custom_label_without_executor_falls_back_to_copilot(self) -> None:
        cfg = ExecutorConfig(provider="auto", foundry=FoundryExecutorConfig(enabled=False))
        protocol = MagicMock()
        protocol.post_task = AsyncMock(return_value=_make_comment())
        dispatcher = ExecutorDispatcher(
            config=cfg, foundry_executor=None, copilot_protocol=protocol
        )
        route = await dispatcher.route(
            pr=_make_pr(labels=[LABEL_AGENT_CUSTOM]),
            copilot_task=_make_copilot_task(),
        )
        assert route.outcome == RouteOutcome.COPILOT_FALLBACK
        protocol.post_task.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_agent_copilot_label_forces_copilot(self) -> None:
        # Even with provider=foundry + enabled executor, the label wins.
        cfg = ExecutorConfig(provider="foundry", foundry=FoundryExecutorConfig(enabled=True))
        protocol = MagicMock()
        protocol.post_task = AsyncMock(return_value=_make_comment())
        foundry = MagicMock()
        foundry.run = AsyncMock()
        dispatcher = ExecutorDispatcher(
            config=cfg, foundry_executor=foundry, copilot_protocol=protocol
        )
        route = await dispatcher.route(
            pr=_make_pr(labels=[LABEL_AGENT_COPILOT]),
            copilot_task=_make_copilot_task(),
        )
        assert route.outcome == RouteOutcome.COPILOT
        foundry.run.assert_not_called()

    @pytest.mark.asyncio
    async def test_explicit_labels_argument_overrides_pr_labels(self) -> None:
        cfg = ExecutorConfig(provider="foundry", foundry=FoundryExecutorConfig(enabled=True))
        protocol = MagicMock()
        protocol.post_task = AsyncMock(return_value=_make_comment())
        foundry = MagicMock()
        foundry.run = AsyncMock()
        dispatcher = ExecutorDispatcher(
            config=cfg, foundry_executor=foundry, copilot_protocol=protocol
        )
        route = await dispatcher.route(
            pr=_make_pr(labels=[LABEL_AGENT_CUSTOM]),
            copilot_task=_make_copilot_task(),
            labels=[LABEL_AGENT_QUARANTINE],
        )
        assert route.outcome == RouteOutcome.REFUSED


class TestDefaultAllowlist:
    def test_default_includes_test_failure(self) -> None:
        cfg = FoundryExecutorConfig()
        assert "TEST_FAILURE" in cfg.allowed_task_types
        assert "LINT_FAILURE" in cfg.allowed_task_types
        assert "REVIEW_COMMENT" in cfg.allowed_task_types
