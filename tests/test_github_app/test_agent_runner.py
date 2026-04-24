"""Tests for RegistryAgentRunner."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from caretaker.agent_protocol import AgentContext, AgentResult
from caretaker.github_app.agent_runner import RegistryAgentRunner
from caretaker.github_app.webhooks import ParsedWebhook


def _make_parsed(
    *,
    delivery: str = "d-0001",
    event: str = "pull_request",
    action: str = "opened",
) -> ParsedWebhook:
    return ParsedWebhook(
        event_type=event,
        delivery_id=delivery,
        action=action,
        installation_id=42,
        repository_full_name="acme/widget",
        payload={"action": action},
    )


def _make_context() -> MagicMock:
    return MagicMock(spec=AgentContext)


def _make_agent(*, name: str = "pr", enabled: bool = True) -> MagicMock:
    agent = MagicMock()
    agent.name = name
    agent.enabled.return_value = enabled
    return agent


# ── RegistryAgentRunner ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_returns_disabled_when_agent_not_in_registry() -> None:
    runner = RegistryAgentRunner()
    registry = MagicMock()
    registry.get.return_value = None

    with patch("caretaker.github_app.agent_runner.build_registry", return_value=registry):
        outcome = await runner.run(
            agent_name="unknown-agent",
            context=_make_context(),
            parsed=_make_parsed(),
        )

    assert outcome == "disabled"
    registry.run_one.assert_not_called()


@pytest.mark.asyncio
async def test_run_returns_disabled_when_agent_reports_disabled() -> None:
    runner = RegistryAgentRunner()
    agent = _make_agent(name="pr", enabled=False)
    registry = MagicMock()
    registry.get.return_value = agent

    with patch("caretaker.github_app.agent_runner.build_registry", return_value=registry):
        outcome = await runner.run(
            agent_name="pr",
            context=_make_context(),
            parsed=_make_parsed(),
        )

    assert outcome == "disabled"
    registry.run_one.assert_not_called()


@pytest.mark.asyncio
async def test_run_returns_success_when_agent_runs_cleanly() -> None:
    runner = RegistryAgentRunner()
    agent = _make_agent(name="pr")
    result = AgentResult(processed=1, actions=["merged pr #1"])
    registry = MagicMock()
    registry.get.return_value = agent
    registry.run_one = AsyncMock(return_value=result)

    with patch("caretaker.github_app.agent_runner.build_registry", return_value=registry):
        outcome = await runner.run(
            agent_name="pr",
            context=_make_context(),
            parsed=_make_parsed(),
        )

    assert outcome == "success"
    registry.run_one.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_returns_failure_when_agent_returns_errors() -> None:
    runner = RegistryAgentRunner()
    agent = _make_agent(name="pr")
    result = AgentResult(errors=["something went wrong"])
    registry = MagicMock()
    registry.get.return_value = agent
    registry.run_one = AsyncMock(return_value=result)

    with patch("caretaker.github_app.agent_runner.build_registry", return_value=registry):
        outcome = await runner.run(
            agent_name="pr",
            context=_make_context(),
            parsed=_make_parsed(),
        )

    assert outcome == "failure"


@pytest.mark.asyncio
async def test_run_returns_failure_when_run_one_returns_none() -> None:
    """registry.run_one returns None when the agent raises internally."""
    runner = RegistryAgentRunner()
    agent = _make_agent(name="pr")
    registry = MagicMock()
    registry.get.return_value = agent
    registry.run_one = AsyncMock(return_value=None)

    with patch("caretaker.github_app.agent_runner.build_registry", return_value=registry):
        outcome = await runner.run(
            agent_name="pr",
            context=_make_context(),
            parsed=_make_parsed(),
        )

    assert outcome == "failure"


@pytest.mark.asyncio
async def test_run_passes_event_payload_to_run_one() -> None:
    runner = RegistryAgentRunner()
    agent = _make_agent(name="pr")
    result = AgentResult(processed=1)
    registry = MagicMock()
    registry.get.return_value = agent
    registry.run_one = AsyncMock(return_value=result)

    parsed = _make_parsed()
    with patch("caretaker.github_app.agent_runner.build_registry", return_value=registry):
        await runner.run(agent_name="pr", context=_make_context(), parsed=parsed)

    _, kwargs = registry.run_one.call_args
    assert kwargs.get("event_payload") == parsed.payload


@pytest.mark.asyncio
async def test_run_builds_fresh_state_and_summary_each_call() -> None:
    """Each dispatch gets isolated ephemeral state — no cross-delivery leakage."""
    from caretaker.state.models import OrchestratorState, RunSummary

    captured_states = []
    captured_summaries = []

    runner = RegistryAgentRunner()
    agent = _make_agent(name="pr")
    registry = MagicMock()
    registry.get.return_value = agent

    async def capture_run_one(ag, state, summary, *, event_payload=None):
        captured_states.append(state)
        captured_summaries.append(summary)
        return AgentResult(processed=1)

    registry.run_one = capture_run_one

    parsed = _make_parsed()
    with patch("caretaker.github_app.agent_runner.build_registry", return_value=registry):
        await runner.run(agent_name="pr", context=_make_context(), parsed=parsed)
        await runner.run(agent_name="pr", context=_make_context(), parsed=parsed)

    assert len(captured_states) == 2
    assert captured_states[0] is not captured_states[1]
    assert isinstance(captured_states[0], OrchestratorState)
    assert isinstance(captured_summaries[0], RunSummary)
