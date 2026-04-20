"""Tests for PRAgent CI fix lifecycle behavior."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from caretaker.config import (
    CIConfig,
    CopilotConfig,
    OwnershipConfig,
    PRAgentConfig,
    ReadinessConfig,
)
from caretaker.github_client.models import (
    CheckConclusion,
    Label,
    PRState,
    ReviewState,
    User,
)
from caretaker.pr_agent.states import ReadinessEvaluation
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
    config.ownership = OwnershipConfig()
    config.readiness = ReadinessConfig()
    return config


def make_readiness_evaluation() -> ReadinessEvaluation:
    """Create a default readiness evaluation for tests."""
    return ReadinessEvaluation(
        score=0.5,
        blockers=["ci_failing"],
        summary="CI failing",
        conclusion="in_progress",
    )


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
            action_required_runs=[],
            all_completed=True,
        )
        evaluation = PRStateEvaluation(
            pr=pr,
            ci=ci_eval,
            reviews=MagicMock(changes_requested=False),
            readiness=make_readiness_evaluation(),
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
            action_required_runs=[],
            all_completed=True,
        )
        evaluation = PRStateEvaluation(
            pr=pr,
            ci=ci_eval,
            reviews=MagicMock(changes_requested=False),
            readiness=make_readiness_evaluation(),
            recommended_state=PRTrackingState.CI_FAILING,
            recommended_action="request_fix",
        )

        report = PRAgentReport()
        updated = await agent._handle_ci_fix(pr, evaluation, tracking, report)

        assert updated.state == PRTrackingState.ESCALATED
        assert 5 in report.escalated
        # Escalation comments are upserted by marker now (Sprint 2 A3) — the
        # body is the 5th positional arg of upsert_issue_comment(owner, repo,
        # number, marker, body).
        github.upsert_issue_comment.assert_awaited()
        comment_body = github.upsert_issue_comment.await_args.args[4]
        assert "Escalation debug dump" in comment_body
        assert '"type": "pr_escalation"' in comment_body
        assert '"max_retries": 2' in comment_body
        assert "<!-- caretaker:escalation -->" in comment_body

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

    async def test_skips_unknown_failure_with_empty_logs(self) -> None:
        """Unknown failure with no log output must NOT post a Copilot task.

        This is the upstream guard that prevents the
        '[WIP] Fix CI failure (unknown)' PR storm: when log extraction
        produced nothing, asking Copilot to "fix nothing" historically
        resulted in noisy speculative PRs that get auto-closed.
        """
        copilot_user = User(login="copilot[bot]", id=1, type="Bot")
        pr = make_pr(number=8, user=copilot_user)
        tracking = TrackedPR(number=8)
        config = make_config(flaky_retries=0)
        # An "unknown"-classified failure: no recognizable patterns and
        # no captured output_summary or output_title.
        failed_run = make_check_run(
            name="cryptic-job",
            conclusion=CheckConclusion.FAILURE,
            output_summary="",
            output_title="",
        )

        updated, report, agent = await self._run_handle_ci_fix(
            pr,
            tracking,
            config,
            failed_run=failed_run,
        )

        agent._copilot_bridge.request_ci_fix.assert_not_awaited()
        assert 8 in report.waiting
        assert updated.copilot_attempts == 0
        assert updated.notes == "skipped_empty_unknown_failure"


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

        with patch.object(
            orchestrator._registry, "run_one", new_callable=AsyncMock
        ) as mock_run_one:
            await orchestrator._handle_event(
                "workflow_run",
                {"workflow_run": {"head_branch": "main", "conclusion": "failure"}},
                state,
                summary,
            )

        # All three agents (pr, devops, self-heal) should run for workflow_run events
        called_names = [call.args[0].name for call in mock_run_one.call_args_list]
        assert "pr" in called_names
        assert "devops" in called_names
        assert "self-heal" in called_names

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

        with patch.object(
            orchestrator._registry, "run_one", new_callable=AsyncMock
        ) as mock_run_one:
            await orchestrator._handle_event(
                "workflow_run",
                {"workflow_run": {"head_branch": "copilot/my-feature", "conclusion": "failure"}},
                state,
                summary,
            )

        # Find the PR agent call and verify head_branch was forwarded
        pr_calls = [call for call in mock_run_one.call_args_list if call.args[0].name == "pr"]
        assert len(pr_calls) == 1
        pr_call = pr_calls[0]
        assert pr_call.kwargs.get("event_payload") == {"_head_branch": "copilot/my-feature"}

    async def test_pr_agent_run_filters_by_head_branch(self) -> None:
        """When head_branch is provided, only PRs on that branch should be processed."""
        from caretaker.pr_agent.agent import PRAgent

        github = AsyncMock()
        from tests.conftest import make_pr as make_test_pr

        pr_target = make_test_pr(number=10, head_ref="copilot/fix-something")
        pr_other = make_test_pr(number=11, head_ref="some-other-branch")

        github.list_pull_requests.return_value = [pr_target, pr_other]
        github.get_check_runs.return_value = []
        github.get_pr_reviews.return_value = []

        config = make_config()
        agent = PRAgent(github=github, owner="o", repo="r", config=config)

        report, _ = await agent.run({}, head_branch="copilot/fix-something")

        # Only the matching PR was evaluated
        assert report.monitored == 1
        github.get_pull_request.assert_not_awaited()


@pytest.mark.asyncio
class TestTrackedPRExternalSync:
    async def test_externally_merged_pr_updates_state_and_merged_at(self) -> None:
        from caretaker.pr_agent.agent import PRAgent
        from tests.conftest import make_pr as make_test_pr

        github = AsyncMock()
        github.list_pull_requests.return_value = []

        merged_at = datetime(2024, 2, 3, tzinfo=UTC)
        closed_pr = make_test_pr(number=42, state=PRState.CLOSED, merged=True)
        closed_pr.merged_at = merged_at
        github.get_pull_request.return_value = closed_pr

        tracked_prs = {
            42: TrackedPR(number=42, state=PRTrackingState.CI_PENDING),
        }

        agent = PRAgent(github=github, owner="o", repo="r", config=make_config())
        _, updated_tracked = await agent.run(tracked_prs)

        assert updated_tracked[42].state == PRTrackingState.MERGED
        assert updated_tracked[42].merged_at == merged_at
        github.get_pull_request.assert_awaited_once_with("o", "r", 42)

    async def test_head_branch_run_does_not_sync_unrelated_tracked_prs(self) -> None:
        from caretaker.pr_agent.agent import PRAgent
        from tests.conftest import make_pr as make_test_pr

        github = AsyncMock()
        branch_pr = make_test_pr(number=101, head_ref="copilot/branch-a")
        github.list_pull_requests.return_value = [branch_pr]
        github.get_check_runs.return_value = []
        github.get_pr_reviews.return_value = []

        tracked_prs = {
            202: TrackedPR(number=202, state=PRTrackingState.CI_PENDING),
        }

        agent = PRAgent(github=github, owner="o", repo="r", config=make_config())
        await agent.run(tracked_prs, head_branch="copilot/branch-a")

        github.get_pull_request.assert_not_awaited()
        assert tracked_prs[202].state == PRTrackingState.CI_PENDING


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
class TestApproveWorkflows:
    """Tests for _handle_approve_workflows."""

    async def test_approve_workflow_run(self) -> None:
        from caretaker.pr_agent.agent import PRAgent, PRAgentReport
        from caretaker.pr_agent.states import CIEvaluation, CIStatus, PRStateEvaluation
        from tests.conftest import make_check_run, make_pr

        pr = make_pr(number=1)
        tracking = TrackedPR(number=1)
        config = make_config()

        github = AsyncMock()
        github.approve_workflow_run.return_value = True

        agent = PRAgent(github=github, owner="o", repo="r", config=config)

        run = make_check_run(
            name="test",
            conclusion=CheckConclusion.ACTION_REQUIRED,
        )
        run.html_url = "https://github.com/owner/repo/actions/runs/12345/jobs/6789"
        ci_eval = CIEvaluation(
            status=CIStatus.PENDING,
            failed_runs=[],
            pending_runs=[],
            passed_runs=[],
            action_required_runs=[run],
            all_completed=True,
        )
        evaluation = PRStateEvaluation(
            pr=pr,
            ci=ci_eval,
            reviews=AsyncMock(),
            readiness=make_readiness_evaluation(),
            recommended_state=PRTrackingState.CI_PENDING,
            recommended_action="approve_workflows",
        )
        report = PRAgentReport()

        updated = await agent._handle_approve_workflows(pr, evaluation, tracking, report)

        github.approve_workflow_run.assert_awaited_once_with("o", "r", 12345)
        assert updated.state == PRTrackingState.CI_PENDING
        assert len(report.waiting) == 0  # Not in waiting if we approved
        assert len(report.errors) == 0

    async def test_approve_workflow_run_failure_adds_error(self) -> None:
        """When approve_workflow_run returns False, the error is recorded in report.errors."""
        from caretaker.pr_agent.agent import PRAgent, PRAgentReport
        from caretaker.pr_agent.states import CIEvaluation, CIStatus, PRStateEvaluation
        from tests.conftest import make_check_run, make_pr

        pr = make_pr(number=1)
        tracking = TrackedPR(number=1)
        config = make_config()

        github = AsyncMock()
        github.approve_workflow_run.return_value = False

        agent = PRAgent(github=github, owner="o", repo="r", config=config)

        run = make_check_run(name="test", conclusion=CheckConclusion.ACTION_REQUIRED)
        run.html_url = "https://github.com/owner/repo/actions/runs/99/jobs/1"
        ci_eval = CIEvaluation(
            status=CIStatus.PENDING,
            failed_runs=[],
            pending_runs=[],
            passed_runs=[],
            action_required_runs=[run],
            all_completed=True,
        )
        evaluation = PRStateEvaluation(
            pr=pr,
            ci=ci_eval,
            reviews=AsyncMock(),
            recommended_state=PRTrackingState.CI_PENDING,
            recommended_action="approve_workflows",
        )
        report = PRAgentReport()

        updated = await agent._handle_approve_workflows(pr, evaluation, tracking, report)

        assert updated.state == PRTrackingState.CI_PENDING
        assert len(report.errors) == 1
        assert "99" in report.errors[0]

    async def test_approve_workflow_run_api_error_adds_error(self) -> None:
        """GitHubAPIError from approve_workflow_run is caught and recorded in report.errors."""
        from caretaker.github_client.api import GitHubAPIError
        from caretaker.pr_agent.agent import PRAgent, PRAgentReport
        from caretaker.pr_agent.states import CIEvaluation, CIStatus, PRStateEvaluation
        from tests.conftest import make_check_run, make_pr

        pr = make_pr(number=2)
        tracking = TrackedPR(number=2)
        config = make_config()

        github = AsyncMock()
        github.approve_workflow_run.side_effect = GitHubAPIError(422, "Unprocessable Entity")

        agent = PRAgent(github=github, owner="o", repo="r", config=config)

        run = make_check_run(name="test", conclusion=CheckConclusion.ACTION_REQUIRED)
        run.html_url = "https://github.com/owner/repo/actions/runs/77/jobs/1"
        ci_eval = CIEvaluation(
            status=CIStatus.PENDING,
            failed_runs=[],
            pending_runs=[],
            passed_runs=[],
            action_required_runs=[run],
            all_completed=True,
        )
        evaluation = PRStateEvaluation(
            pr=pr,
            ci=ci_eval,
            reviews=AsyncMock(),
            recommended_state=PRTrackingState.CI_PENDING,
            recommended_action="approve_workflows",
        )
        report = PRAgentReport()

        updated = await agent._handle_approve_workflows(pr, evaluation, tracking, report)

        assert updated.state == PRTrackingState.CI_PENDING
        assert len(report.errors) == 1
        assert "77" in report.errors[0]

    async def test_approve_workflow_run_extract_failure(self) -> None:
        from caretaker.pr_agent.agent import PRAgent, PRAgentReport
        from caretaker.pr_agent.states import CIEvaluation, CIStatus, PRStateEvaluation
        from tests.conftest import make_check_run, make_pr

        pr = make_pr(number=1)
        tracking = TrackedPR(number=1)
        config = make_config()

        github = AsyncMock()

        agent = PRAgent(github=github, owner="o", repo="r", config=config)

        run = make_check_run(
            name="test",
            conclusion=CheckConclusion.ACTION_REQUIRED,
        )
        run.html_url = "https://github.com/owner/repo/invalid/url"
        ci_eval = CIEvaluation(
            status=CIStatus.PENDING,
            failed_runs=[],
            pending_runs=[],
            passed_runs=[],
            action_required_runs=[run],
            all_completed=True,
        )
        evaluation = PRStateEvaluation(
            pr=pr,
            ci=ci_eval,
            reviews=AsyncMock(),
            recommended_state=PRTrackingState.CI_PENDING,
            recommended_action="approve_workflows",
        )
        report = PRAgentReport()

        updated = await agent._handle_approve_workflows(pr, evaluation, tracking, report)

        github.approve_workflow_run.assert_not_awaited()
        assert updated.state == PRTrackingState.CI_PENDING
        assert 1 in report.waiting


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
            action_required_runs=[],
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


# ── Comment deduplication ────────────────────────────────────────────


@pytest.mark.asyncio
class TestCommentDeduplication:
    """Tests for _has_pending_task_comment and its integration with fix handlers."""

    async def test_no_comments_means_no_pending_task(self) -> None:
        """When no comments exist, there is no pending task."""
        from caretaker.pr_agent.agent import PRAgent

        github = AsyncMock()
        github.get_pr_comments.return_value = []
        config = make_config()
        agent = PRAgent(github=github, owner="o", repo="r", config=config)

        assert await agent._has_pending_task_comment(1) is False

    async def test_task_without_result_is_pending(self) -> None:
        """A task comment with no subsequent result is pending."""
        from caretaker.pr_agent.agent import PRAgent
        from tests.conftest import make_comment

        github = AsyncMock()
        github.get_pr_comments.return_value = [
            make_comment(body="<!-- caretaker:task -->Fix CI failure"),
        ]
        config = make_config()
        agent = PRAgent(github=github, owner="o", repo="r", config=config)

        assert await agent._has_pending_task_comment(1) is True

    async def test_task_followed_by_result_is_not_pending(self) -> None:
        """A task comment that has been answered by a result is not pending."""
        from caretaker.pr_agent.agent import PRAgent
        from tests.conftest import make_comment

        github = AsyncMock()
        github.get_pr_comments.return_value = [
            make_comment(body="<!-- caretaker:task -->Fix CI failure"),
            make_comment(body="<!-- caretaker:result -->FIXED"),
        ]
        config = make_config()
        agent = PRAgent(github=github, owner="o", repo="r", config=config)

        assert await agent._has_pending_task_comment(1) is False

    async def test_multiple_tasks_last_unanswered_is_pending(self) -> None:
        """When there are multiple tasks, pending status depends on the last one."""
        from caretaker.pr_agent.agent import PRAgent
        from tests.conftest import make_comment

        github = AsyncMock()
        github.get_pr_comments.return_value = [
            make_comment(body="<!-- caretaker:task -->First task"),
            make_comment(body="<!-- caretaker:result -->FIXED"),
            make_comment(body="<!-- caretaker:task -->Second task"),
        ]
        config = make_config()
        agent = PRAgent(github=github, owner="o", repo="r", config=config)

        assert await agent._has_pending_task_comment(1) is True

    async def test_ci_fix_skips_when_task_pending(self) -> None:
        """_handle_ci_fix should skip posting when a task comment is already pending."""
        from caretaker.pr_agent.agent import PRAgent, PRAgentReport
        from caretaker.pr_agent.states import CIEvaluation, CIStatus, PRStateEvaluation
        from tests.conftest import make_comment

        copilot_user = User(login="copilot[bot]", id=1, type="Bot")
        pr = make_pr(number=100, user=copilot_user)
        tracking = TrackedPR(number=100)
        config = make_config()

        github = AsyncMock()
        github.get_pr_comments.return_value = [
            make_comment(body="<!-- caretaker:task -->Fix CI failure"),
        ]
        agent = PRAgent(github=github, owner="o", repo="r", config=config)

        failed_run = make_check_run(name="lint", conclusion=CheckConclusion.FAILURE)
        ci_eval = CIEvaluation(
            status=CIStatus.FAILING,
            failed_runs=[failed_run],
            pending_runs=[],
            passed_runs=[],
            action_required_runs=[],
            all_completed=True,
        )
        evaluation = PRStateEvaluation(
            pr=pr,
            ci=ci_eval,
            reviews=MagicMock(changes_requested=False),
            recommended_state=PRTrackingState.CI_FAILING,
            recommended_action="request_fix",
        )

        mock_result = MagicMock()
        mock_result.comment_id = 99
        agent._copilot_bridge.request_ci_fix = AsyncMock(return_value=mock_result)

        report = PRAgentReport()
        updated = await agent._handle_ci_fix(pr, evaluation, tracking, report)

        # No new fix comment should have been posted
        agent._copilot_bridge.request_ci_fix.assert_not_awaited()
        assert updated.state == PRTrackingState.FIX_REQUESTED
        assert 100 in report.waiting
        assert report.fix_requested == []

    async def test_review_fix_skips_when_task_pending(self) -> None:
        """_handle_review_fix should skip posting when a task comment is already pending."""
        from caretaker.pr_agent.agent import PRAgent, PRAgentReport
        from tests.conftest import make_comment

        pr = make_pr(number=101, user=User(login="copilot[bot]", id=1, type="Bot"))
        tracking = TrackedPR(number=101)
        config = make_config()

        github = AsyncMock()
        github.get_pr_comments.return_value = [
            make_comment(body="<!-- caretaker:task -->Fix review comments"),
        ]
        agent = PRAgent(github=github, owner="o", repo="r", config=config)

        bot_review = make_review(
            user=User(login="copilot-pull-request-reviewer", id=99, type="Bot"),
            state=ReviewState.COMMENTED,
            body="Consider fixing this.",
        )

        mock_result = MagicMock()
        mock_result.comment_id = 42
        agent._copilot_bridge.request_review_fix = AsyncMock(return_value=mock_result)

        report = PRAgentReport()
        updated = await agent._handle_review_fix(pr, [bot_review], tracking, report)

        # No new fix comment should have been posted
        agent._copilot_bridge.request_review_fix.assert_not_awaited()
        assert updated.state == PRTrackingState.FIX_REQUESTED
        assert 101 in report.waiting
        assert report.fix_requested == []

    async def test_ci_fix_posts_when_previous_task_answered(self) -> None:
        """_handle_ci_fix should post normally when the previous task was answered."""
        from caretaker.pr_agent.agent import PRAgent, PRAgentReport
        from caretaker.pr_agent.states import CIEvaluation, CIStatus, PRStateEvaluation
        from tests.conftest import make_comment

        copilot_user = User(login="copilot[bot]", id=1, type="Bot")
        pr = make_pr(number=102, user=copilot_user)
        tracking = TrackedPR(number=102)
        config = make_config()

        github = AsyncMock()
        github.get_pr_comments.return_value = [
            make_comment(body="<!-- caretaker:task -->Fix CI failure"),
            make_comment(body="<!-- caretaker:result -->FIXED"),
        ]
        agent = PRAgent(github=github, owner="o", repo="r", config=config)

        failed_run = make_check_run(name="lint", conclusion=CheckConclusion.FAILURE)
        ci_eval = CIEvaluation(
            status=CIStatus.FAILING,
            failed_runs=[failed_run],
            pending_runs=[],
            passed_runs=[],
            action_required_runs=[],
            all_completed=True,
        )
        evaluation = PRStateEvaluation(
            pr=pr,
            ci=ci_eval,
            reviews=MagicMock(changes_requested=False),
            recommended_state=PRTrackingState.CI_FAILING,
            recommended_action="request_fix",
        )

        mock_result = MagicMock()
        mock_result.comment_id = 99
        agent._copilot_bridge.request_ci_fix = AsyncMock(return_value=mock_result)

        report = PRAgentReport()
        updated = await agent._handle_ci_fix(pr, evaluation, tracking, report)

        # Fix comment should have been posted since previous was answered
        agent._copilot_bridge.request_ci_fix.assert_awaited_once()
        assert updated.copilot_attempts == 1
        assert 102 in report.fix_requested


# ── PR-number single-PR fast path ─────────────────────────────────────────────


@pytest.mark.asyncio
class TestPRNumberFastPath:
    """Tests for the pr_number parameter that scopes a run to a single PR."""

    async def test_pr_number_fetches_single_pr_instead_of_listing(self) -> None:
        """When pr_number is given, list_pull_requests must not be called."""
        from caretaker.pr_agent.agent import PRAgent
        from tests.conftest import make_pr as make_test_pr

        github = AsyncMock()
        target_pr = make_test_pr(number=5, head_ref="copilot/fix")
        github.get_pull_request.return_value = target_pr
        github.get_check_runs.return_value = []
        github.get_pr_reviews.return_value = []

        agent = PRAgent(github=github, owner="o", repo="r", config=make_config())
        report, _ = await agent.run({}, pr_number=5)

        github.list_pull_requests.assert_not_awaited()
        github.get_pull_request.assert_awaited_once_with("o", "r", 5)
        assert report.monitored == 1

    async def test_pr_number_closed_pr_is_skipped(self) -> None:
        """If the fetched PR is closed, return early without processing."""
        from caretaker.pr_agent.agent import PRAgent
        from tests.conftest import make_pr as make_test_pr

        github = AsyncMock()
        closed_pr = make_test_pr(number=7, state=PRState.CLOSED)
        github.get_pull_request.return_value = closed_pr

        agent = PRAgent(github=github, owner="o", repo="r", config=make_config())
        report, tracked = await agent.run({}, pr_number=7)

        assert report.monitored == 0
        github.get_check_runs.assert_not_awaited()

    async def test_pr_number_not_found_returns_empty(self) -> None:
        """If get_pull_request returns None, return early with no work done."""
        from caretaker.pr_agent.agent import PRAgent

        github = AsyncMock()
        github.get_pull_request.return_value = None

        agent = PRAgent(github=github, owner="o", repo="r", config=make_config())
        report, tracked = await agent.run({}, pr_number=99)

        assert report.monitored == 0
        github.get_check_runs.assert_not_awaited()

    async def test_pr_number_skips_external_sync(self) -> None:
        """Single-PR fast path must not sync state for unrelated tracked PRs."""
        from caretaker.pr_agent.agent import PRAgent
        from tests.conftest import make_pr as make_test_pr

        github = AsyncMock()
        target_pr = make_test_pr(number=5, head_ref="copilot/fix")
        github.get_pull_request.return_value = target_pr
        github.get_check_runs.return_value = []
        github.get_pr_reviews.return_value = []

        tracked_prs = {
            200: TrackedPR(number=200, state=PRTrackingState.CI_PENDING),
        }

        agent = PRAgent(github=github, owner="o", repo="r", config=make_config())
        await agent.run(tracked_prs, pr_number=5)

        # The unrelated tracked PR must not have been synced
        assert tracked_prs[200].state == PRTrackingState.CI_PENDING
        # get_pull_request should only have been called once (for the target PR)
        github.get_pull_request.assert_awaited_once_with("o", "r", 5)

    async def test_pr_number_takes_precedence_over_head_branch_and_warns(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """When both pr_number and head_branch are supplied, pr_number wins and a
        warning is logged to alert the caller of the unexpected combination."""
        import logging

        from caretaker.pr_agent.agent import PRAgent
        from tests.conftest import make_pr as make_test_pr

        github = AsyncMock()
        target_pr = make_test_pr(number=5, head_ref="copilot/fix")
        github.get_pull_request.return_value = target_pr
        github.get_check_runs.return_value = []
        github.get_pr_reviews.return_value = []

        agent = PRAgent(github=github, owner="o", repo="r", config=make_config())

        with caplog.at_level(logging.WARNING, logger="caretaker.pr_agent.agent"):
            report, _ = await agent.run({}, pr_number=5, head_branch="some-branch")

        # pr_number path was used (list not called, single PR fetched)
        github.list_pull_requests.assert_not_awaited()
        github.get_pull_request.assert_awaited_once_with("o", "r", 5)
        assert report.monitored == 1
        # Warning was emitted about the unexpected combination
        assert any(
            "pr_number" in record.message and "head_branch" in record.message
            for record in caplog.records
            if record.levelno == logging.WARNING
        )


# ── _handle_event PR number extraction ──────────────────────────────────────


@pytest.mark.asyncio
class TestHandleEventPRNumberExtraction:
    """Tests that _handle_event extracts and forwards PR numbers for PR events."""

    def _make_orchestrator(self):
        from caretaker.config import MaintainerConfig
        from caretaker.orchestrator import Orchestrator

        github = AsyncMock()
        config = MaintainerConfig()
        return Orchestrator(config=config, github=github, owner="o", repo="r")

    async def test_pull_request_event_forwards_pr_number(self) -> None:
        """pull_request event extracts pull_request.number into _pr_number."""
        from caretaker.state.models import OrchestratorState, RunSummary

        orchestrator = self._make_orchestrator()
        state = OrchestratorState()
        summary = RunSummary(mode="event")

        with patch.object(
            orchestrator._registry, "run_one", new_callable=AsyncMock
        ) as mock_run_one:
            await orchestrator._handle_event(
                "pull_request",
                {"pull_request": {"number": 42, "action": "opened"}},
                state,
                summary,
            )

        pr_calls = [c for c in mock_run_one.call_args_list if c.args[0].name == "pr"]
        assert len(pr_calls) == 1
        assert pr_calls[0].kwargs.get("event_payload") == {"_pr_number": 42}

    async def test_pull_request_review_event_forwards_pr_number(self) -> None:
        """pull_request_review event extracts pull_request.number into _pr_number."""
        from caretaker.state.models import OrchestratorState, RunSummary

        orchestrator = self._make_orchestrator()
        state = OrchestratorState()
        summary = RunSummary(mode="event")

        with patch.object(
            orchestrator._registry, "run_one", new_callable=AsyncMock
        ) as mock_run_one:
            await orchestrator._handle_event(
                "pull_request_review",
                {"pull_request": {"number": 55}, "review": {"state": "approved"}},
                state,
                summary,
            )

        pr_calls = [c for c in mock_run_one.call_args_list if c.args[0].name == "pr"]
        assert len(pr_calls) == 1
        assert pr_calls[0].kwargs.get("event_payload") == {"_pr_number": 55}

    async def test_check_run_event_forwards_pr_number(self) -> None:
        """check_run event extracts first PR number from check_run.pull_requests."""
        from caretaker.state.models import OrchestratorState, RunSummary

        orchestrator = self._make_orchestrator()
        state = OrchestratorState()
        summary = RunSummary(mode="event")

        with patch.object(
            orchestrator._registry, "run_one", new_callable=AsyncMock
        ) as mock_run_one:
            await orchestrator._handle_event(
                "check_run",
                {"check_run": {"pull_requests": [{"number": 77}]}},
                state,
                summary,
            )

        pr_calls = [c for c in mock_run_one.call_args_list if c.args[0].name == "pr"]
        assert len(pr_calls) == 1
        assert pr_calls[0].kwargs.get("event_payload") == {"_pr_number": 77}

    async def test_check_run_no_pr_falls_back_to_full_scan(self) -> None:
        """check_run with no linked PR runs a full PR scan (empty event_payload)."""
        from caretaker.state.models import OrchestratorState, RunSummary

        orchestrator = self._make_orchestrator()
        state = OrchestratorState()
        summary = RunSummary(mode="event")

        with patch.object(
            orchestrator._registry, "run_one", new_callable=AsyncMock
        ) as mock_run_one:
            await orchestrator._handle_event(
                "check_run",
                {"check_run": {"pull_requests": []}},
                state,
                summary,
            )

        pr_calls = [c for c in mock_run_one.call_args_list if c.args[0].name == "pr"]
        assert len(pr_calls) == 1
        # No PR number — full scan payload
        assert pr_calls[0].kwargs.get("event_payload") == {}

    async def test_check_suite_event_forwards_pr_number(self) -> None:
        """check_suite event extracts first PR number from check_suite.pull_requests."""
        from caretaker.state.models import OrchestratorState, RunSummary

        orchestrator = self._make_orchestrator()
        state = OrchestratorState()
        summary = RunSummary(mode="event")

        with patch.object(
            orchestrator._registry, "run_one", new_callable=AsyncMock
        ) as mock_run_one:
            await orchestrator._handle_event(
                "check_suite",
                {"check_suite": {"pull_requests": [{"number": 33}]}},
                state,
                summary,
            )

        pr_calls = [c for c in mock_run_one.call_args_list if c.args[0].name == "pr"]
        assert len(pr_calls) == 1
        assert pr_calls[0].kwargs.get("event_payload") == {"_pr_number": 33}
