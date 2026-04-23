"""Tests for ``PRCIApproverAgent`` — surfacing and approving stuck bot CI runs.

Coverage:
- config defaults
- filters: actor whitelist, trigger-event allowlist, max_age_hours cutoff
- auto_approve=False surfaces only (no API write)
- auto_approve=True calls approve_workflow_run
- graceful failure on the expected 403 "not a fork PR" response
- apply_summary writes to the correct RunSummary fields
- registry registration + event routing
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest

from caretaker.agent_protocol import AgentContext
from caretaker.config import MaintainerConfig, PRCIApproverConfig
from caretaker.github_client.api import GitHubAPIError
from caretaker.pr_ci_approver.agent import PRCIApproverAgent
from caretaker.state.models import OrchestratorState, RunSummary

# ── helpers ───────────────────────────────────────────────────────────────


def _make_run(
    *,
    run_id: int = 1,
    actor: str = "Copilot",
    event: str = "pull_request",
    age_hours: float = 1.0,
    workflow_name: str = "CI",
    head_branch: str = "copilot/fix-xyz",
) -> dict[str, Any]:
    """Build a workflow-run payload shaped like the GitHub REST response."""
    created_at = (datetime.now(UTC) - timedelta(hours=age_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "id": run_id,
        "name": workflow_name,
        "event": event,
        "status": "completed",
        "conclusion": "action_required",
        "head_branch": head_branch,
        "created_at": created_at,
        "actor": {"login": actor},
        "triggering_actor": {"login": actor},
    }


def _make_ctx(cfg: PRCIApproverConfig | None = None) -> AgentContext:
    """Construct an AgentContext with an AsyncMock GitHub client."""
    mc = MaintainerConfig()
    if cfg is not None:
        mc = mc.model_copy(update={"pr_ci_approver": cfg})
    return AgentContext(
        github=AsyncMock(),
        owner="o",
        repo="r",
        config=mc,
        llm_router=None,  # type: ignore[arg-type]
    )


# ── config ────────────────────────────────────────────────────────────────


def test_pr_ci_approver_defaults_are_safe() -> None:
    cfg = PRCIApproverConfig()
    assert cfg.enabled is True
    # Default is auto_approve=True — tight allowed_actors list (bots only) makes
    # this safe and avoids upgrade PRs stalling forever on a manual UI click.
    assert cfg.auto_approve is True
    assert cfg.max_age_hours == 48
    assert cfg.max_runs_per_run == 25
    # Whitelist must include Copilot's usual logins and known bots.
    for expected in ("Copilot", "github-actions[bot]", "dependabot[bot]"):
        assert expected in cfg.allowed_actors
    # pull_request is the primary trigger; issue_comment covers @-mentions.
    assert "pull_request" in cfg.trigger_events
    assert "issue_comment" in cfg.trigger_events


def test_pr_ci_approver_in_maintainer_config() -> None:
    mc = MaintainerConfig()
    assert isinstance(mc.pr_ci_approver, PRCIApproverConfig)
    assert mc.pr_ci_approver.enabled is True


# ── agent behaviour ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_surfaces_stuck_copilot_run_without_approving_when_disabled() -> None:
    """Explicit auto_approve=False → count the run, don't call approve."""
    ctx = _make_ctx(PRCIApproverConfig(auto_approve=False))
    ctx.github.list_workflow_runs = AsyncMock(return_value=[_make_run()])  # type: ignore[method-assign]
    ctx.github.approve_workflow_run = AsyncMock()  # type: ignore[method-assign]

    agent = PRCIApproverAgent(ctx)
    result = await agent.execute(OrchestratorState())

    assert result.processed == 1
    assert result.extra["runs_stuck"] == 1
    assert result.extra["runs_approved"] == 0
    assert result.extra["runs_surfaced"] == 1
    ctx.github.approve_workflow_run.assert_not_awaited()


@pytest.mark.asyncio
async def test_auto_approve_true_by_default_calls_approve_endpoint() -> None:
    """Default auto_approve=True → approve endpoint is called for bot runs."""
    ctx = _make_ctx()  # uses PRCIApproverConfig() defaults
    ctx.github.list_workflow_runs = AsyncMock(return_value=[_make_run(run_id=55)])  # type: ignore[method-assign]
    ctx.github.approve_workflow_run = AsyncMock(return_value=True)  # type: ignore[method-assign]

    agent = PRCIApproverAgent(ctx)
    result = await agent.execute(OrchestratorState())

    ctx.github.approve_workflow_run.assert_awaited_once_with("o", "r", 55)
    assert result.extra["runs_approved"] == 1
    assert result.extra["runs_surfaced"] == 0


@pytest.mark.asyncio
async def test_auto_approve_true_calls_approve_endpoint() -> None:
    cfg = PRCIApproverConfig(auto_approve=True)
    ctx = _make_ctx(cfg)
    ctx.github.list_workflow_runs = AsyncMock(return_value=[_make_run(run_id=42)])  # type: ignore[method-assign]
    ctx.github.approve_workflow_run = AsyncMock(return_value=True)  # type: ignore[method-assign]

    agent = PRCIApproverAgent(ctx)
    result = await agent.execute(OrchestratorState())

    ctx.github.approve_workflow_run.assert_awaited_once_with("o", "r", 42)
    assert result.extra["runs_approved"] == 1
    assert result.extra["runs_surfaced"] == 0


@pytest.mark.asyncio
async def test_auto_approve_handles_expected_403_gracefully() -> None:
    """The approve endpoint 403s for same-repo bot PRs ('not a fork pull
    request'). The agent must not crash — it should count the run as
    surfaced and continue."""
    cfg = PRCIApproverConfig(auto_approve=True)
    ctx = _make_ctx(cfg)
    ctx.github.list_workflow_runs = AsyncMock(return_value=[_make_run(run_id=7)])  # type: ignore[method-assign]
    ctx.github.approve_workflow_run = AsyncMock(  # type: ignore[method-assign]
        side_effect=GitHubAPIError(403, "This run is not from a fork pull request")
    )

    agent = PRCIApproverAgent(ctx)
    result = await agent.execute(OrchestratorState())

    assert result.extra["runs_stuck"] == 1
    assert result.extra["runs_approved"] == 0
    assert result.extra["runs_surfaced"] == 1
    # The error is captured per-run but doesn't bubble up as an AgentResult
    # error (the agent completed its intended work).
    assert result.errors == []
    details = result.extra["details"]
    assert details and "403" in (details[0]["approval_error"] or "")


@pytest.mark.asyncio
async def test_actor_not_in_whitelist_is_skipped() -> None:
    """Runs triggered by non-whitelisted actors must be ignored."""
    ctx = _make_ctx()
    ctx.github.list_workflow_runs = AsyncMock(  # type: ignore[method-assign]
        return_value=[_make_run(actor="some-random-user")]
    )
    agent = PRCIApproverAgent(ctx)
    result = await agent.execute(OrchestratorState())
    assert result.processed == 0


@pytest.mark.asyncio
async def test_event_not_in_trigger_events_is_skipped() -> None:
    cfg = PRCIApproverConfig(trigger_events=["pull_request"])
    ctx = _make_ctx(cfg)
    ctx.github.list_workflow_runs = AsyncMock(  # type: ignore[method-assign]
        return_value=[_make_run(event="schedule")]
    )
    agent = PRCIApproverAgent(ctx)
    result = await agent.execute(OrchestratorState())
    assert result.processed == 0


@pytest.mark.asyncio
async def test_run_older_than_max_age_is_skipped() -> None:
    """Stale runs (e.g., superseded by a newer push) should not be
    retroactively approved — we'd be rubber-stamping an ancient SHA."""
    cfg = PRCIApproverConfig(max_age_hours=24, auto_approve=True)
    ctx = _make_ctx(cfg)
    ctx.github.list_workflow_runs = AsyncMock(  # type: ignore[method-assign]
        return_value=[_make_run(age_hours=72.0)]
    )
    ctx.github.approve_workflow_run = AsyncMock()  # type: ignore[method-assign]
    agent = PRCIApproverAgent(ctx)
    result = await agent.execute(OrchestratorState())
    assert result.processed == 0
    ctx.github.approve_workflow_run.assert_not_awaited()


@pytest.mark.asyncio
async def test_list_failure_returns_empty_result_with_error() -> None:
    """A listing failure must not crash the whole caretaker run."""
    ctx = _make_ctx()
    ctx.github.list_workflow_runs = AsyncMock(  # type: ignore[method-assign]
        side_effect=GitHubAPIError(500, "server error")
    )
    agent = PRCIApproverAgent(ctx)
    result = await agent.execute(OrchestratorState())
    assert result.processed == 0
    assert result.errors and "list_workflow_runs failed" in result.errors[0]


@pytest.mark.asyncio
async def test_dry_run_never_calls_approve() -> None:
    cfg = PRCIApproverConfig(auto_approve=True)
    ctx = _make_ctx(cfg)
    ctx.dry_run = True
    ctx.github.list_workflow_runs = AsyncMock(return_value=[_make_run()])  # type: ignore[method-assign]
    ctx.github.approve_workflow_run = AsyncMock()  # type: ignore[method-assign]
    agent = PRCIApproverAgent(ctx)
    result = await agent.execute(OrchestratorState())
    assert result.extra["runs_stuck"] == 1
    assert result.extra["runs_approved"] == 0
    ctx.github.approve_workflow_run.assert_not_awaited()


def test_apply_summary_writes_expected_fields() -> None:
    ctx = _make_ctx()
    agent = PRCIApproverAgent(ctx)
    summary = RunSummary()
    from caretaker.agent_protocol import AgentResult

    result = AgentResult(
        processed=3,
        extra={"runs_stuck": 3, "runs_approved": 1, "runs_surfaced": 2},
    )
    agent.apply_summary(result, summary)
    assert summary.ci_runs_stuck == 3
    assert summary.ci_runs_approved == 1
    assert summary.ci_runs_surfaced == 2


# ── registry wiring ───────────────────────────────────────────────────────


def test_agent_is_registered_in_full_mode() -> None:
    from caretaker.agents import AGENT_MODES

    assert "pr-ci-approver" in AGENT_MODES
    assert "full" in AGENT_MODES["pr-ci-approver"]


def test_agent_is_mapped_to_pull_request_event() -> None:
    from caretaker.agents import EVENT_AGENT_MAP

    assert "pr-ci-approver" in EVENT_AGENT_MAP["pull_request"]
    assert "pr-ci-approver" in EVENT_AGENT_MAP["workflow_run"]
