"""Tests for PRAgent CI fix lifecycle behavior."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from caretaker.config import CIConfig, CopilotConfig, PRAgentConfig
from caretaker.github_client.models import (
    CheckConclusion,
    Label,
    ReviewState,
    User,
)
from caretaker.state.models import PRTrackingState, TrackedPR
from tests.conftest import make_check_run, make_pr, make_review


def make_config(
    flaky_retries: int = 1,
    max_retries: int = 2,
    close_managed_prs_on_backlog: bool = False,
) -> PRAgentConfig:
    config = PRAgentConfig()
    config.ci = CIConfig(
        flaky_retries=flaky_retries,
        close_managed_prs_on_backlog=close_managed_prs_on_backlog,
    )
    config.copilot = CopilotConfig(max_retries=max_retries)
    return config


@pytest.mark.asyncio
class TestCIFixLifecycle:
    """Tests for _handle_ci_fix — flaky retry bypass and fix request."""

    async def _run_handle_ci_fix(
        self,
        pr,
        tracking: TrackedPR,
        config: PRAgentConfig,
        failed_run=None,
    ) -> tuple:
        """Helper: invoke _handle_ci_fix and return (tracking, report)."""
        from caretaker.pr_agent.agent import PRAgent, PRAgentReport
        from caretaker.pr_agent.states import CIEvaluation, CIStatus, PRStateEvaluation

        github = AsyncMock()
        agent = PRAgent(github=github, owner="o", repo="r", config=config)

        if failed_run is None:
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

    async def test_managed_pr_with_backlog_failure_is_closed_when_enabled(self) -> None:
        """A backlog-guard failure should close managed PRs when configured."""
        copilot_user = User(login="copilot[bot]", id=1, type="Bot")
        pr = make_pr(number=6, user=copilot_user)
        tracking = TrackedPR(number=6)
        config = make_config(flaky_retries=0, close_managed_prs_on_backlog=True)
        failed_run = make_check_run(
            name="queue-guard",
            conclusion=CheckConclusion.FAILURE,
            output_summary="CI backlog guard tripped",
        )

        updated, report, agent = await self._run_handle_ci_fix(
            pr,
            tracking,
            config,
            failed_run=failed_run,
        )

        agent._copilot_bridge.request_ci_fix.assert_not_awaited()
        agent._github.add_issue_comment.assert_awaited_once()
        agent._github.update_issue.assert_awaited_once_with("o", "r", 6, state="closed")
        assert updated.state == PRTrackingState.CLOSED
        assert report.fix_requested == []

    async def test_human_pr_with_backlog_failure_stays_open(self) -> None:
        """A backlog-guard failure should not auto-close human PRs."""
        human_user = User(login="dev", id=5, type="User")
        pr = make_pr(number=7, user=human_user)
        tracking = TrackedPR(number=7)
        config = make_config(flaky_retries=0, close_managed_prs_on_backlog=True)
        failed_run = make_check_run(
            name="queue-guard",
            conclusion=CheckConclusion.FAILURE,
            output_summary="CI backlog guard tripped",
        )

        updated, report, agent = await self._run_handle_ci_fix(
            pr,
            tracking,
            config,
            failed_run=failed_run,
        )

        agent._copilot_bridge.request_ci_fix.assert_not_awaited()
        agent._github.add_issue_comment.assert_not_awaited()
        agent._github.update_issue.assert_not_awaited()
        assert updated.state == PRTrackingState.DISCOVERED
        assert 7 in report.waiting


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

    async def test_workflow_run_event_forwards_head_branch(self) -> None:
        """head_branch from workflow_run payload must be forwarded to the PR agent."""
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
            patch.object(orchestrator, "_run_devops_agent", new_callable=AsyncMock),
            patch.object(orchestrator, "_run_self_heal_agent", new_callable=AsyncMock),
        ):
            await orchestrator._handle_event(
                "workflow_run",
                {"workflow_run": {"head_branch": "copilot/my-feature", "conclusion": "failure"}},
                state,
                summary,
            )

        # The PR agent must receive the specific branch so only that PR is scanned
        mock_pr.assert_awaited_once_with(state, summary, head_branch="copilot/my-feature")

    async def test_pr_agent_run_filters_by_head_branch(self) -> None:
        """When head_branch is provided, only PRs on that branch should be processed."""
        from caretaker.pr_agent.agent import PRAgent

        github = AsyncMock()
        from tests.conftest import make_pr as make_test_pr

        pr_target = make_test_pr(number=10)
        pr_target.head_ref = "copilot/fix-something"  # type: ignore[attr-defined]

        pr_other = make_test_pr(number=11)
        pr_other.head_ref = "some-other-branch"  # type: ignore[attr-defined]

        github.list_pull_requests.return_value = [pr_target, pr_other]
        github.get_check_runs.return_value = []
        github.get_pr_reviews.return_value = []

        config = make_config()
        agent = PRAgent(github=github, owner="o", repo="r", config=config)

        report, _ = await agent.run({}, head_branch="copilot/fix-something")

        # Only the matching PR was evaluated
        assert report.monitored == 1


# ── is_copilot_pr recognition ────────────────────────────────────────


class TestIsCopilotPR:
    """Verify that is_copilot_pr covers all known Copilot bot logins."""

    def test_copilot_swe_agent_bot_is_recognized(self) -> None:
        pr = make_pr(user=User(login="copilot-swe-agent[bot]", id=1, type="Bot"))
        assert pr.is_copilot_pr

    def test_copilot_bot_is_recognized(self) -> None:
        pr = make_pr(user=User(login="copilot[bot]", id=1, type="Bot"))
        assert pr.is_copilot_pr

    def test_github_copilot_bot_is_recognized(self) -> None:
        pr = make_pr(user=User(login="github-copilot[bot]", id=1, type="Bot"))
        assert pr.is_copilot_pr

    def test_human_is_not_copilot(self) -> None:
        pr = make_pr(user=User(login="dev", id=5, type="User"))
        assert not pr.is_copilot_pr


# ── _handle_review_fix — no author gating ─────────────────────────────


@pytest.mark.asyncio
class TestReviewFixLifecycle:
    """Tests for _handle_review_fix — review fix request for any PR."""

    async def _run_handle_review_fix(self, pr, tracking: TrackedPR, config: PRAgentConfig) -> tuple:
        from caretaker.pr_agent.agent import PRAgent, PRAgentReport

        github = AsyncMock()
        agent = PRAgent(github=github, owner="o", repo="r", config=config)

        bot_review = make_review(
            user=User(login="copilot-pull-request-reviewer", id=99, type="Bot"),
            state=ReviewState.COMMENTED,
            body="Consider reconciling state before skipping.",
        )

        mock_result = MagicMock()
        mock_result.comment_id = 42
        agent._copilot_bridge.request_review_fix = AsyncMock(return_value=mock_result)

        report = PRAgentReport()
        updated = await agent._handle_review_fix(pr, [bot_review], tracking, report)
        return updated, report, agent

    async def test_human_pr_gets_review_fix_requested(self) -> None:
        """A human-authored PR must also get review fix requests (no author gating)."""
        pr = make_pr(number=20, user=User(login="dev", id=5, type="User"))
        tracking = TrackedPR(number=20)
        config = make_config()

        updated, report, agent = await self._run_handle_review_fix(pr, tracking, config)

        agent._copilot_bridge.request_review_fix.assert_awaited_once()
        assert updated.state == PRTrackingState.FIX_REQUESTED
        assert 20 in report.fix_requested

    async def test_copilot_swe_agent_pr_gets_review_fix_requested(self) -> None:
        """copilot-swe-agent[bot] authored PR gets review fix requests."""
        pr = make_pr(number=21, user=User(login="copilot-swe-agent[bot]", id=1, type="Bot"))
        tracking = TrackedPR(number=21)
        config = make_config()

        updated, report, agent = await self._run_handle_review_fix(pr, tracking, config)

        agent._copilot_bridge.request_review_fix.assert_awaited_once()
        assert updated.state == PRTrackingState.FIX_REQUESTED

    async def test_maintainer_pr_gets_review_fix_requested(self) -> None:
        """Maintainer-labeled PR gets review fix requests."""
        pr = make_pr(number=22, labels=[Label(name="maintainer:managed", color="")])
        tracking = TrackedPR(number=22)
        config = make_config()

        updated, report, agent = await self._run_handle_review_fix(pr, tracking, config)

        agent._copilot_bridge.request_review_fix.assert_awaited_once()
        assert updated.state == PRTrackingState.FIX_REQUESTED


# ── _process_pr state persistence ─────────────────────────────────────


@pytest.mark.asyncio
class TestProcessPRStatePersistence:
    """Verify that action handlers' state overrides are not clobbered."""

    async def test_fix_requested_state_persists_after_review_fix(self) -> None:
        """After _handle_review_fix sets FIX_REQUESTED, it must not be
        overwritten back to REVIEW_CHANGES_REQUESTED by _process_pr."""
        from caretaker.pr_agent.agent import PRAgent

        github = AsyncMock()
        config = make_config()
        agent = PRAgent(github=github, owner="o", repo="r", config=config)

        # Set up a PR with passing CI and automated review comments
        pr = make_pr(number=30)
        checks = [make_check_run(name="test")]
        bot_review = make_review(
            user=User(login="copilot-pull-request-reviewer", id=99, type="Bot"),
            state=ReviewState.COMMENTED,
            body="Consider reconciling state.",
        )

        github.get_check_runs.return_value = checks
        github.get_pr_reviews.return_value = [bot_review]

        mock_result = MagicMock()
        mock_result.comment_id = 42
        agent._copilot_bridge.request_review_fix = AsyncMock(return_value=mock_result)

        from caretaker.pr_agent.agent import PRAgentReport

        tracking = TrackedPR(number=30)
        report = PRAgentReport()
        updated = await agent._process_pr(pr, tracking, report)

        # The handler sets FIX_REQUESTED — it must persist
        assert updated.state == PRTrackingState.FIX_REQUESTED


@pytest.mark.asyncio
class TestHandleMergeBranchProtection:
    """Tests for _handle_merge when GitHub rejects merge due to branch-protection rules."""

    async def _run_handle_merge(
        self,
        pr,
        tracking: TrackedPR,
        config: PRAgentConfig,
        merge_side_effect=None,
    ) -> tuple:
        """Helper: invoke _handle_merge and return (tracking, report, agent)."""
        from caretaker.pr_agent.agent import PRAgent, PRAgentReport
        from caretaker.pr_agent.states import (
            CIEvaluation,
            CIStatus,
            PRStateEvaluation,
            ReviewEvaluation,
        )

        github = AsyncMock()
        if merge_side_effect is not None:
            github.merge_pull_request.side_effect = merge_side_effect
        else:
            github.merge_pull_request.return_value = True

        agent = PRAgent(github=github, owner="o", repo="r", config=config)

        ci_eval = CIEvaluation(
            status=CIStatus.PASSING,
            failed_runs=[],
            pending_runs=[],
            passed_runs=[make_check_run(name="lint", conclusion=CheckConclusion.SUCCESS)],
            all_completed=True,
        )
        review_eval = ReviewEvaluation(
            changes_requested=False,
            approved=True,
            pending=False,
            blocking_reviews=[],
            approving_reviews=[],
        )
        evaluation = PRStateEvaluation(
            pr=pr,
            ci=ci_eval,
            reviews=review_eval,
            recommended_state=PRTrackingState.MERGE_READY,
            recommended_action="merge",
        )

        report = PRAgentReport()
        updated_tracking = await agent._handle_merge(pr, evaluation, tracking, report)
        return updated_tracking, report, agent

    async def test_405_branch_protection_adds_to_waiting_not_errors(self) -> None:
        """A 405 from GitHub (branch protection) must not propagate as an error."""
        from caretaker.github_client.api import GitHubAPIError

        copilot_user = User(login="copilot[bot]", id=1, type="Bot")
        pr = make_pr(number=10, user=copilot_user)
        tracking = TrackedPR(number=10)
        config = make_config()

        exc = GitHubAPIError(
            405,
            '{"message":"Repository rule violations found'
            '\\n\\nAt least 1 approving review is required."}',
        )
        updated, report, _ = await self._run_handle_merge(
            pr, tracking, config, merge_side_effect=exc
        )

        assert 10 in report.waiting
        assert report.errors == []

    async def test_409_conflict_adds_to_waiting_not_errors(self) -> None:
        """A 409 from GitHub (merge conflict) must not propagate as an error."""
        from caretaker.github_client.api import GitHubAPIError

        copilot_user = User(login="copilot[bot]", id=1, type="Bot")
        pr = make_pr(number=11, user=copilot_user)
        tracking = TrackedPR(number=11)
        config = make_config()

        exc = GitHubAPIError(409, '{"message":"Merge conflict"}')
        updated, report, _ = await self._run_handle_merge(
            pr, tracking, config, merge_side_effect=exc
        )

        assert 11 in report.waiting
        assert report.errors == []

    async def test_unexpected_api_error_is_re_raised(self) -> None:
        """A GitHubAPIError with an unexpected status code must be re-raised."""
        from caretaker.github_client.api import GitHubAPIError

        copilot_user = User(login="copilot[bot]", id=1, type="Bot")
        pr = make_pr(number=12, user=copilot_user)
        tracking = TrackedPR(number=12)
        config = make_config()

        exc = GitHubAPIError(500, '{"message":"Internal Server Error"}')
        with pytest.raises(GitHubAPIError) as exc_info:
            await self._run_handle_merge(pr, tracking, config, merge_side_effect=exc)

        assert exc_info.value.status_code == 500
