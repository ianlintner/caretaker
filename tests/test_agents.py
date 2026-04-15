"""Tests for agent adapters."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from caretaker.agent_protocol import AgentContext
from caretaker.agents import SelfHealAgentAdapter
from caretaker.config import MaintainerConfig
from caretaker.state.models import OrchestratorState


@pytest.mark.asyncio
async def test_self_heal_adapter_persists_actioned_sigs_in_stable_order() -> None:
    ctx = AgentContext(
        github=AsyncMock(),
        owner="o",
        repo="r",
        config=MaintainerConfig(),
        llm_router=None,  # type: ignore[arg-type]
    )
    adapter = SelfHealAgentAdapter(ctx)
    state = OrchestratorState(reported_self_heal_sigs=["old-1", "old-2"])

    mock_report = SimpleNamespace(
        failures_analyzed=1,
        errors=[],
        local_issues_created=[],
        upstream_issues_opened=[],
        upstream_features_requested=[],
        actioned_sigs=["old-2", "new-1", "new-2"],
    )

    with patch("caretaker.agents.SelfHealAgent") as mock_agent_cls:
        mock_agent_cls.return_value.run = AsyncMock(return_value=mock_report)
        await adapter.execute(state)

    assert state.reported_self_heal_sigs == ["old-1", "old-2", "new-1", "new-2"]


@pytest.mark.asyncio
async def test_self_heal_adapter_caps_to_latest_500_while_preserving_order() -> None:
    ctx = AgentContext(
        github=AsyncMock(),
        owner="o",
        repo="r",
        config=MaintainerConfig(),
        llm_router=None,  # type: ignore[arg-type]
    )
    adapter = SelfHealAgentAdapter(ctx)
    existing = [f"sig-{i}" for i in range(500)]
    state = OrchestratorState(reported_self_heal_sigs=existing)

    mock_report = SimpleNamespace(
        failures_analyzed=1,
        errors=[],
        local_issues_created=[],
        upstream_issues_opened=[],
        upstream_features_requested=[],
        actioned_sigs=["sig-10", "sig-500", "sig-501"],
    )

    with patch("caretaker.agents.SelfHealAgent") as mock_agent_cls:
        mock_agent_cls.return_value.run = AsyncMock(return_value=mock_report)
        await adapter.execute(state)

    expected = [f"sig-{i}" for i in range(2, 500)] + ["sig-500", "sig-501"]
    assert state.reported_self_heal_sigs == expected
