"""Tests for RegistryAgentRunner."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from caretaker.agent_protocol import AgentContext, AgentResult
from caretaker.github_app.agent_runner import RegistryAgentRunner
from caretaker.github_app.webhooks import ParsedWebhook
from caretaker.state.models import OrchestratorState, TrackedPR


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


def _make_context(owner: str = "acme", repo: str = "widget") -> MagicMock:
    ctx = MagicMock(spec=AgentContext)
    ctx.github = MagicMock()
    ctx.owner = owner
    ctx.repo = repo
    return ctx


def _make_agent(*, name: str = "pr", enabled: bool = True) -> MagicMock:
    agent = MagicMock()
    agent.name = name
    agent.enabled.return_value = enabled
    return agent


def _make_tracker(*, state: OrchestratorState | None = None) -> MagicMock:
    tracker = MagicMock()
    tracker.load = AsyncMock(return_value=state or OrchestratorState())
    tracker.save = AsyncMock()
    return tracker


# ── RegistryAgentRunner ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_returns_disabled_when_agent_not_in_registry() -> None:
    runner = RegistryAgentRunner()
    registry = MagicMock()
    registry.get.return_value = None
    tracker = _make_tracker()

    with (
        patch("caretaker.github_app.agent_runner.build_registry", return_value=registry),
        patch("caretaker.github_app.agent_runner.StateTracker", return_value=tracker),
    ):
        outcome = await runner.run(
            agent_name="unknown-agent",
            context=_make_context(),
            parsed=_make_parsed(),
        )

    assert outcome == "disabled"
    registry.run_one.assert_not_called()
    # State not loaded/saved when agent doesn't exist
    tracker.load.assert_not_called()
    tracker.save.assert_not_called()


@pytest.mark.asyncio
async def test_run_returns_disabled_when_agent_reports_disabled() -> None:
    runner = RegistryAgentRunner()
    agent = _make_agent(name="pr", enabled=False)
    registry = MagicMock()
    registry.get.return_value = agent
    tracker = _make_tracker()

    with (
        patch("caretaker.github_app.agent_runner.build_registry", return_value=registry),
        patch("caretaker.github_app.agent_runner.StateTracker", return_value=tracker),
    ):
        outcome = await runner.run(
            agent_name="pr",
            context=_make_context(),
            parsed=_make_parsed(),
        )

    assert outcome == "disabled"
    registry.run_one.assert_not_called()
    # State not loaded/saved when agent is disabled
    tracker.load.assert_not_called()
    tracker.save.assert_not_called()


@pytest.mark.asyncio
async def test_run_returns_success_when_agent_runs_cleanly() -> None:
    runner = RegistryAgentRunner()
    agent = _make_agent(name="pr")
    result = AgentResult(processed=1, actions=["merged pr #1"])
    registry = MagicMock()
    registry.get.return_value = agent
    registry.run_one = AsyncMock(return_value=result)
    tracker = _make_tracker()

    with (
        patch("caretaker.github_app.agent_runner.build_registry", return_value=registry),
        patch("caretaker.github_app.agent_runner.StateTracker", return_value=tracker),
    ):
        outcome = await runner.run(
            agent_name="pr",
            context=_make_context(),
            parsed=_make_parsed(),
        )

    assert outcome == "success"
    registry.run_one.assert_awaited_once()
    tracker.load.assert_awaited_once()
    tracker.save.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_returns_failure_when_agent_returns_errors() -> None:
    runner = RegistryAgentRunner()
    agent = _make_agent(name="pr")
    result = AgentResult(errors=["something went wrong"])
    registry = MagicMock()
    registry.get.return_value = agent
    registry.run_one = AsyncMock(return_value=result)
    tracker = _make_tracker()

    with (
        patch("caretaker.github_app.agent_runner.build_registry", return_value=registry),
        patch("caretaker.github_app.agent_runner.StateTracker", return_value=tracker),
    ):
        outcome = await runner.run(
            agent_name="pr",
            context=_make_context(),
            parsed=_make_parsed(),
        )

    assert outcome == "failure"
    tracker.load.assert_awaited_once()
    tracker.save.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_returns_failure_when_run_one_returns_none() -> None:
    """registry.run_one returns None when the agent raises internally."""
    runner = RegistryAgentRunner()
    agent = _make_agent(name="pr")
    registry = MagicMock()
    registry.get.return_value = agent
    registry.run_one = AsyncMock(return_value=None)
    tracker = _make_tracker()

    with (
        patch("caretaker.github_app.agent_runner.build_registry", return_value=registry),
        patch("caretaker.github_app.agent_runner.StateTracker", return_value=tracker),
    ):
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
    tracker = _make_tracker()

    parsed = _make_parsed()
    with (
        patch("caretaker.github_app.agent_runner.build_registry", return_value=registry),
        patch("caretaker.github_app.agent_runner.StateTracker", return_value=tracker),
    ):
        await runner.run(agent_name="pr", context=_make_context(), parsed=parsed)

    _, kwargs = registry.run_one.call_args
    assert kwargs.get("event_payload") == parsed.payload


@pytest.mark.asyncio
async def test_run_loads_persisted_state_for_each_delivery() -> None:
    """Each delivery gets its own StateTracker load — state is persisted, not shared in-memory."""
    captured_states = []

    runner = RegistryAgentRunner()
    agent = _make_agent(name="pr")
    registry = MagicMock()
    registry.get.return_value = agent

    async def capture_run_one(ag, state, summary, *, event_payload=None):
        captured_states.append(state)
        return AgentResult(processed=1)

    registry.run_one = capture_run_one

    # Simulate two sequential webhook deliveries with distinct persisted states
    state_a = OrchestratorState(tracked_prs={1: TrackedPR(number=1, ci_attempts=1)})
    state_b = OrchestratorState(tracked_prs={2: TrackedPR(number=2, ci_attempts=3)})
    tracker_a = _make_tracker(state=state_a)
    tracker_b = _make_tracker(state=state_b)

    parsed = _make_parsed()
    with patch("caretaker.github_app.agent_runner.build_registry", return_value=registry):
        with patch("caretaker.github_app.agent_runner.StateTracker", return_value=tracker_a):
            await runner.run(agent_name="pr", context=_make_context(), parsed=parsed)
        with patch("caretaker.github_app.agent_runner.StateTracker", return_value=tracker_b):
            await runner.run(agent_name="pr", context=_make_context(), parsed=parsed)

    assert len(captured_states) == 2
    # Different state instances, loaded from tracker each time
    assert captured_states[0] is not captured_states[1]
    assert captured_states[0].tracked_prs[1].ci_attempts == 1
    assert captured_states[1].tracked_prs[2].ci_attempts == 3


@pytest.mark.asyncio
async def test_run_falls_back_to_fresh_state_when_load_fails() -> None:
    """State load failure is non-fatal — run proceeds with empty OrchestratorState."""
    runner = RegistryAgentRunner()
    agent = _make_agent(name="pr")
    result = AgentResult(processed=1)
    registry = MagicMock()
    registry.get.return_value = agent
    registry.run_one = AsyncMock(return_value=result)

    tracker = MagicMock()
    tracker.load = AsyncMock(side_effect=RuntimeError("network error"))
    tracker.save = AsyncMock()

    with (
        patch("caretaker.github_app.agent_runner.build_registry", return_value=registry),
        patch("caretaker.github_app.agent_runner.StateTracker", return_value=tracker),
    ):
        outcome = await runner.run(
            agent_name="pr",
            context=_make_context(),
            parsed=_make_parsed(),
        )

    # Agent still ran despite load failure
    assert outcome == "success"
    registry.run_one.assert_awaited_once()
    # State passed to run_one is a fresh OrchestratorState
    positional, _ = registry.run_one.call_args
    assert isinstance(positional[1], OrchestratorState)


@pytest.mark.asyncio
async def test_run_still_succeeds_when_save_fails() -> None:
    """State save failure is non-fatal — success/failure reflects agent outcome only."""
    runner = RegistryAgentRunner()
    agent = _make_agent(name="pr")
    result = AgentResult(processed=1)
    registry = MagicMock()
    registry.get.return_value = agent
    registry.run_one = AsyncMock(return_value=result)

    tracker = MagicMock()
    tracker.load = AsyncMock(return_value=OrchestratorState())
    tracker.save = AsyncMock(side_effect=RuntimeError("rate limited"))

    with (
        patch("caretaker.github_app.agent_runner.build_registry", return_value=registry),
        patch("caretaker.github_app.agent_runner.StateTracker", return_value=tracker),
    ):
        outcome = await runner.run(
            agent_name="pr",
            context=_make_context(),
            parsed=_make_parsed(),
        )

    assert outcome == "success"
