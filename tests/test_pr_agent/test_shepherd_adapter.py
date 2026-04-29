"""Tests for the ShepherdAgentAdapter + registry + mode wiring."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from caretaker.agent_protocol import AgentContext
from caretaker.agents import ShepherdAgentAdapter
from caretaker.agents._registry_data import AGENT_MODES, ALL_ADAPTERS, build_registry
from caretaker.cli import RunMode
from caretaker.config import MaintainerConfig
from caretaker.pr_agent.shepherd import ShepherdReport
from caretaker.state.models import OrchestratorState, RunSummary


def _ctx(*, shepherd_enabled: bool = False, dry_run: bool = False) -> AgentContext:
    cfg = MaintainerConfig()
    cfg.shepherd.enabled = shepherd_enabled
    return AgentContext(
        github=AsyncMock(),
        owner="o",
        repo="r",
        config=cfg,
        llm_router=None,  # type: ignore[arg-type]
        dry_run=dry_run,
    )


def test_shepherd_adapter_registered_for_shepherd_mode_only() -> None:
    assert ShepherdAgentAdapter in ALL_ADAPTERS
    assert AGENT_MODES["shepherd"] == {"shepherd"}
    # Must NOT leak into "full" or any other existing mode
    for mode_key in (
        "full",
        "pr-only",
        "issue-only",
        "upgrade",
        "self-heal",
        "devops",
        "stale",
        "escalation",
    ):
        assert "shepherd" not in AGENT_MODES.get(  # type: ignore[operator]
            mode_key, set()
        ), f"shepherd should not appear in mode '{mode_key}'"


def test_shepherd_runmode_enum_has_shepherd() -> None:
    assert RunMode("shepherd") is RunMode.SHEPHERD


def test_build_registry_registers_shepherd_under_shepherd_mode_only() -> None:
    ctx = _ctx()
    registry = build_registry(ctx)
    shepherd_agents = [a.name for a in registry.agents_for_mode("shepherd")]
    full_agents = [a.name for a in registry.agents_for_mode("full")]
    assert "shepherd" in shepherd_agents
    assert "shepherd" not in full_agents


def test_shepherd_adapter_disabled_when_config_off() -> None:
    adapter = ShepherdAgentAdapter(_ctx(shepherd_enabled=False))
    assert adapter.enabled() is False


def test_shepherd_adapter_enabled_when_config_on() -> None:
    adapter = ShepherdAgentAdapter(_ctx(shepherd_enabled=True))
    assert adapter.enabled() is True


@pytest.mark.asyncio
async def test_shepherd_adapter_execute_returns_agent_result_from_report() -> None:
    adapter = ShepherdAgentAdapter(_ctx(shepherd_enabled=True))
    report = ShepherdReport(
        inventoried=5,
        enriched=5,
        closed_duplicate=[101, 102],
        promoted=[201],
        mechanical_fixed=[301],
        rebased=[401],
        closed_stale=[501],
        merged=[601, 602],
        llm_budget_used=0,
        skipped_phases=["mechanical_fixes:pending-delta-c"],
        errors=[],
    )
    with patch(
        "caretaker.pr_agent.shepherd_adapter.run_shepherd",
        new=AsyncMock(return_value=report),
    ):
        result = await adapter.execute(OrchestratorState())
    # action_count sums 6 action lists: 2+1+1+1+1+2 = 8
    assert result.processed == 8
    assert result.errors == []
    assert result.extra["closed_duplicate"] == [101, 102]
    assert result.extra["promoted"] == [201]
    assert result.extra["rebased"] == [401]
    assert result.extra["closed_stale"] == [501]
    assert result.extra["merged"] == [601, 602]
    assert result.extra["llm_budget_used"] == 0
    assert "mechanical_fixes:pending-delta-c" in result.extra["skipped_phases"]


@pytest.mark.asyncio
async def test_shepherd_adapter_execute_propagates_errors() -> None:
    adapter = ShepherdAgentAdapter(_ctx(shepherd_enabled=True))
    report = ShepherdReport(errors=["inventory: boom"])
    with patch(
        "caretaker.pr_agent.shepherd_adapter.run_shepherd",
        new=AsyncMock(return_value=report),
    ):
        result = await adapter.execute(OrchestratorState())
    assert result.errors == ["inventory: boom"]
    assert result.processed == 0


@pytest.mark.asyncio
async def test_shepherd_adapter_passes_context_dry_run_into_run_shepherd() -> None:
    adapter = ShepherdAgentAdapter(_ctx(shepherd_enabled=True, dry_run=True))
    fake = AsyncMock(return_value=ShepherdReport())
    with patch("caretaker.pr_agent.shepherd_adapter.run_shepherd", new=fake):
        await adapter.execute(OrchestratorState())
    _, kwargs = fake.call_args
    assert kwargs["dry_run"] is True


@pytest.mark.asyncio
async def test_shepherd_adapter_passes_none_when_not_dry_run() -> None:
    """When ctx.dry_run=False, pass None so run_shepherd falls back to config.dry_run."""
    adapter = ShepherdAgentAdapter(_ctx(shepherd_enabled=True, dry_run=False))
    fake = AsyncMock(return_value=ShepherdReport())
    with patch("caretaker.pr_agent.shepherd_adapter.run_shepherd", new=fake):
        await adapter.execute(OrchestratorState())
    _, kwargs = fake.call_args
    assert kwargs["dry_run"] is None


def test_shepherd_adapter_apply_summary_folds_merged_into_prs_merged() -> None:
    adapter = ShepherdAgentAdapter(_ctx(shepherd_enabled=True))
    summary = RunSummary(mode="shepherd", prs_merged=2)
    result = SimpleNamespace(
        processed=0,
        errors=[],
        state_updates={},
        extra={"merged": [701, 702, 703]},
    )
    adapter.apply_summary(result, summary)  # type: ignore[arg-type]
    assert summary.prs_merged == 5


def test_shepherd_mode_not_in_event_agent_map() -> None:
    """Shepherd is scheduled-only: it must not react to GitHub events."""
    from caretaker.agents._registry_data import EVENT_AGENT_MAP

    for event_type, agents in EVENT_AGENT_MAP.items():
        assert "shepherd" not in agents, (
            f"shepherd leaked into event {event_type!r} agents {agents}"
        )


def test_dogfood_config_has_shepherd_section_disabled() -> None:
    """Dogfood config must declare shepherd explicitly disabled so
    behavior is byte-identical to pre-Delta-E runs until operator opts in."""
    from pathlib import Path

    import yaml

    root = Path(__file__).resolve().parents[2]
    cfg_path = root / ".github" / "maintainer" / "config.yml"
    cfg = yaml.safe_load(cfg_path.read_text())
    assert "shepherd" in cfg, "dogfood config missing shepherd block"
    assert cfg["shepherd"]["enabled"] is False
