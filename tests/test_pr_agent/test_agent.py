"""Tests for PRAgent CI fix lifecycle behavior."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from caretaker.config import CIConfig, CopilotConfig, PRAgentConfig
from caretaker.github_client.models import (
    CheckConclusion,
    Label,
    User,
)
from caretaker.state.models import PRTrackingState, TrackedPR
from tests.conftest import make_check_run, make_pr


def make_config(flaky_retries: int = 1, max_retries: int = 2) -> PRAgentConfig:
    config = PRAgentConfig()
    config.ci = CIConfig(flaky_retries=flaky_retries)
    config.copilot = CopilotConfig(max_retries=max_retries)
    return config


@pytest.mark.asyncio
class TestCIFixLifecycle:
    """Tests for _handle_ci_fix — flaky retry bypass and fix request."""

    async def _run_handle_ci_fix(self, pr, tracking: TrackedPR, config: PRAgentConfig) -> tuple:
        """Helper: invoke _handle_ci_fix and return (tracking, report)."""
        from caretaker.pr_agent.agent import PRAgent, PRAgentReport
        from caretaker.pr_agent.states import CIEvaluation, CIStatus, PRStateEvaluation

        github = AsyncMock()
        agent = PRAgent(github=github, owner="o", repo="r", config=config)

        failed_run = make_check_run(name="lint", conclusion=CheckConclusion.FAILURE)
        ci_eval = CIEvaluation(
            status=CIStatus.FAILING,
            failed_runs=[failed_run],
            pending_runs=[],
            passed_runs=[],
            all_completed=True,
        )
        evaluation = PRStateEvaluation(
            pr=pr,
            ci=ci_eval,
            reviews=MagicMock(changes_requested=False),
            recommended_state=PRTrackingState.CI_FAILING,
            recommended_action="request_fix",
        )

        # Stub the bridge so we don't hit real GitHub
        mock_result = MagicMock()
        mock_result.comment_id = 99
        agent._copilot_bridge.request_ci_fix = AsyncMock(return_value=mock_result)
        agent._copilot_bridge._protocol._github = github

        report = PRAgentReport()
        updated_tracking = await agent._handle_ci_fix(pr, evaluation, tracking, report)
        return updated_tracking, report, agent

    async def test_copilot_pr_skips_flaky_retry_and_requests_fix_immediately(
        self,
    ) -> None:
        """A Copilot-authored PR must post a fix request on the first CI failure."""
        copilot_user = User(login="copilot[bot]", id=1, type="Bot")
        pr = make_pr(number=1, user=copilot_user)
        tracking = TrackedPR(number=1)
        config = make_config(flaky_retries=1)

        updated, report, agent = await self._run_handle_ci_fix(pr, tracking, config)

        # Fix request should have been posted, not just waited
        agent._copilot_bridge.request_ci_fix.assert_awaited_once()
        assert updated.copilot_attempts == 1
        assert 1 in report.fix_requested

    async def test_maintainer_pr_skips_flaky_retry_and_requests_fix_immediately(
        self,
    ) -> None:
        """A PR carrying a maintainer: label must also skip the flaky retry."""
        pr = make_pr(number=2, labels=[Label(name="maintainer:assigned", color="")])
        tracking = TrackedPR(number=2)
        config = make_config(flaky_retries=1)

        updated, report, agent = await self._run_handle_ci_fix(pr, tracking, config)

        agent._copilot_bridge.request_ci_fix.assert_awaited_once()
        assert updated.copilot_attempts == 1
        assert 2 in report.fix_requested

    async def test_human_pr_waits_on_first_failure(self) -> None:
        """A human-authored PR waits one cycle on the first CI failure (flaky retry)."""
        human_user = User(login="dev", id=5, type="User")
        pr = make_pr(number=3, user=human_user)
        tracking = TrackedPR(number=3, ci_attempts=0)
        config = make_config(flaky_retries=1)

        updated, report, agent = await self._run_handle_ci_fix(pr, tracking, config)

        # Should have waited (no fix request yet)
        agent._copilot_bridge.request_ci_fix.assert_not_awaited()
        assert updated.ci_attempts == 1
        assert 3 in report.waiting

    async def test_human_pr_requests_fix_after_flaky_retry_exhausted(self) -> None:
        """After flaky_retries are used up, the fix request must be posted."""
        human_user = User(login="dev", id=5, type="User")
        pr = make_pr(number=4, user=human_user)
        # ci_attempts already = flaky_retries (1), so bypass the wait
        tracking = TrackedPR(number=4, ci_attempts=1)
        config = make_config(flaky_retries=1)

        updated, report, agent = await self._run_handle_ci_fix(pr, tracking, config)

        agent._copilot_bridge.request_ci_fix.assert_awaited_once()
        assert updated.copilot_attempts == 1
        assert 4 in report.fix_requested

    async def test_escalates_when_copilot_max_retries_reached(self) -> None:
        """Once copilot_attempts >= max_retries, the PR must be escalated."""
        copilot_user = User(login="copilot[bot]", id=1, type="Bot")
        pr = make_pr(number=5, user=copilot_user)
        tracking = TrackedPR(number=5, copilot_attempts=2)
        config = make_config(max_retries=2)

        from caretaker.pr_agent.agent import PRAgent, PRAgentReport
        from caretaker.pr_agent.states import CIEvaluation, CIStatus, PRStateEvaluation

        github = AsyncMock()
        agent = PRAgent(github=github, owner="o", repo="r", config=config)

        failed_run = make_check_run(name="lint", conclusion=CheckConclusion.FAILURE)
        ci_eval = CIEvaluation(
            status=CIStatus.FAILING,
            failed_runs=[failed_run],
            pending_runs=[],
            passed_runs=[],
            all_completed=True,
        )
        evaluation = PRStateEvaluation(
            pr=pr,
            ci=ci_eval,
            reviews=MagicMock(changes_requested=False),
            recommended_state=PRTrackingState.CI_FAILING,
            recommended_action="request_fix",
        )

        report = PRAgentReport()
        updated = await agent._handle_ci_fix(pr, evaluation, tracking, report)

        assert updated.state == PRTrackingState.ESCALATED
        assert 5 in report.escalated


@pytest.mark.asyncio
class TestOrchestratorWorkflowRunEvent:
    """Tests that workflow_run events also trigger the PR agent."""

    async def test_workflow_run_event_runs_pr_agent(self) -> None:
        """When event_type is workflow_run, the PR agent must run."""
        from caretaker.config import MaintainerConfig
        from caretaker.orchestrator import Orchestrator
        from caretaker.state.models import OrchestratorState, RunSummary

        github = AsyncMock()
        config = MaintainerConfig()
        orchestrator = Orchestrator(config=config, github=github, owner="o", repo="r")

        state = OrchestratorState()
        summary = RunSummary(mode="event")

        with (
            patch.object(orchestrator, "_run_pr_agent", new_callable=AsyncMock) as mock_pr,
            patch.object(orchestrator, "_run_devops_agent", new_callable=AsyncMock) as mock_devops,
            patch.object(orchestrator, "_run_self_heal_agent", new_callable=AsyncMock) as mock_heal,
        ):
            await orchestrator._handle_event(
                "workflow_run",
                {"workflow_run": {"head_branch": "main", "conclusion": "failure"}},
                state,
                summary,
            )

        mock_pr.assert_awaited_once()
        mock_devops.assert_awaited_once()
        mock_heal.assert_awaited_once()
