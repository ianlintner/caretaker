"""Tests for PRAgent CI fix lifecycle behavior."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from caretaker.config import (
    CIConfig,
    CopilotConfig,
    MergeAuthorityConfig,
    MergeAuthorityMode,
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
        assert "caretaker:causal" in comment_body
        assert "source=pr-agent:escalation" in comment_body

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

    async def test_sync_loop_does_not_shadow_pr_number_parameter(self) -> None:
        """Regression for T-S2: the closed-PR sync loop previously reused
        ``pr_number`` as its loop variable, shadowing the outer parameter.

        With multiple closed PRs in tracked_prs, each one must be fetched by
        its own number (not the last-iterated loop variable) and updated
        independently. This exercises the renamed loop variable.
        """
        from caretaker.pr_agent.agent import PRAgent
        from tests.conftest import make_pr as make_test_pr

        github = AsyncMock()
        github.list_pull_requests.return_value = []  # empty scan

        merged_at_a = datetime(2024, 2, 1, tzinfo=UTC)
        merged_at_b = datetime(2024, 2, 2, tzinfo=UTC)
        pr_a = make_test_pr(number=11, state=PRState.CLOSED, merged=True)
        pr_a.merged_at = merged_at_a
        pr_b = make_test_pr(number=22, state=PRState.CLOSED, merged=True)
        pr_b.merged_at = merged_at_b

        # get_pull_request must be answered per PR number — if the sync
        # loop confused the outer parameter with its loop variable it could
        # request the same number twice.
        async def _get_pr(owner: str, repo: str, number: int):
            return {11: pr_a, 22: pr_b}[number]

        github.get_pull_request.side_effect = _get_pr

        tracked_prs = {
            11: TrackedPR(number=11, state=PRTrackingState.CI_PENDING),
            22: TrackedPR(number=22, state=PRTrackingState.CI_PENDING),
        }

        agent = PRAgent(github=github, owner="o", repo="r", config=make_config())
        _, updated_tracked = await agent.run(tracked_prs)

        assert updated_tracked[11].state == PRTrackingState.MERGED
        assert updated_tracked[11].merged_at == merged_at_a
        assert updated_tracked[22].state == PRTrackingState.MERGED
        assert updated_tracked[22].merged_at == merged_at_b
        # Both numbers were requested — proves the loop iterated each entry
        # by its own key.
        called_numbers = sorted(call.args[2] for call in github.get_pull_request.await_args_list)
        assert called_numbers == [11, 22]


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

    async def test_reversed_order_still_returns_correct_pending_verdict(self) -> None:
        """Regression for T-S7: get_pr_comments does not guarantee ordering.

        When the API returns comments in reverse chronological order, the
        previous implementation's "last task before any result" logic could
        invert. The function must sort explicitly by (created_at, id) so the
        verdict is stable regardless of input order.
        """
        from caretaker.github_client.models import Comment, User
        from caretaker.pr_agent.agent import PRAgent

        user = User(login="caretaker[bot]", id=99, type="Bot")
        # Task first (older), then result (newer): the correct verdict is
        # NOT pending — the task has been answered.
        task = Comment(
            id=1,
            user=user,
            body="<!-- caretaker:task -->Fix CI failure",
            created_at=datetime(2024, 1, 1, 12, 0, tzinfo=UTC),
        )
        result = Comment(
            id=2,
            user=user,
            body="<!-- caretaker:result -->FIXED",
            created_at=datetime(2024, 1, 1, 13, 0, tzinfo=UTC),
        )

        github = AsyncMock()
        # API returns them in reverse chronological order.
        github.get_pr_comments.return_value = [result, task]
        config = make_config()
        agent = PRAgent(github=github, owner="o", repo="r", config=config)

        assert await agent._has_pending_task_comment(1) is False

    async def test_reversed_order_detects_unanswered_task(self) -> None:
        """When the API returns an unanswered task among older comments in
        reverse order, the verdict must be *pending* (True)."""
        from caretaker.github_client.models import Comment, User
        from caretaker.pr_agent.agent import PRAgent

        user = User(login="caretaker[bot]", id=99, type="Bot")
        # First task, then result, then second task (most recent). Correct
        # verdict: pending (second task has no subsequent result).
        first_task = Comment(
            id=1,
            user=user,
            body="<!-- caretaker:task -->First task",
            created_at=datetime(2024, 1, 1, 12, 0, tzinfo=UTC),
        )
        result = Comment(
            id=2,
            user=user,
            body="<!-- caretaker:result -->FIXED",
            created_at=datetime(2024, 1, 1, 13, 0, tzinfo=UTC),
        )
        second_task = Comment(
            id=3,
            user=user,
            body="<!-- caretaker:task -->Second task",
            created_at=datetime(2024, 1, 1, 14, 0, tzinfo=UTC),
        )

        github = AsyncMock()
        # Reverse chronological order.
        github.get_pr_comments.return_value = [second_task, result, first_task]
        config = make_config()
        agent = PRAgent(github=github, owner="o", repo="r", config=config)

        assert await agent._has_pending_task_comment(1) is True


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


@pytest.mark.asyncio
class TestStuckPRAgeGate:
    """Sprint 3 E1: escalate PRs open longer than stuck_age_hours that have
    no human approval. Catches portfolio #4 (10 days), #28 (7 days)."""

    async def _run_process_pr(self, pr, tracking, config, *, reviews=None):
        from caretaker.pr_agent.agent import PRAgent, PRAgentReport

        github = AsyncMock()
        github.get_check_runs = AsyncMock(return_value=[])
        github.get_pr_reviews = AsyncMock(return_value=reviews or [])
        agent = PRAgent(github=github, owner="o", repo="r", config=config)
        report = PRAgentReport()
        updated = await agent._process_pr(pr, tracking, report)
        return updated, report, agent

    async def test_old_pr_with_no_human_approval_escalates(self) -> None:
        from datetime import timedelta

        pr = make_pr(
            number=1,
            user=User(login="copilot[bot]", id=1, type="Bot"),
            created_at=datetime.now(UTC) - timedelta(hours=48),  # 48h old, threshold 24h
        )
        tracking = TrackedPR(number=1)
        config = make_config()
        config.stuck_age_hours = 24

        updated, report, _ = await self._run_process_pr(pr, tracking, config)

        assert updated.state == PRTrackingState.ESCALATED
        assert updated.escalated is True
        assert 1 in report.escalated

    async def test_recent_pr_does_not_escalate(self) -> None:
        from datetime import timedelta

        pr = make_pr(
            number=2,
            user=User(login="copilot[bot]", id=1, type="Bot"),
            created_at=datetime.now(UTC) - timedelta(hours=2),  # well under 24h
        )
        tracking = TrackedPR(number=2)
        config = make_config()
        config.stuck_age_hours = 24

        updated, report, _ = await self._run_process_pr(pr, tracking, config)
        assert updated.state != PRTrackingState.ESCALATED
        assert 2 not in report.escalated

    async def test_old_pr_with_human_approval_does_not_escalate(self) -> None:
        from datetime import timedelta

        pr = make_pr(
            number=3,
            user=User(login="copilot[bot]", id=1, type="Bot"),
            created_at=datetime.now(UTC) - timedelta(days=5),
        )
        human_approval = make_review(
            user=User(login="ian", id=10, type="User"),
            state=ReviewState.APPROVED,
        )
        tracking = TrackedPR(number=3)
        config = make_config()
        config.stuck_age_hours = 24

        updated, report, _ = await self._run_process_pr(
            pr, tracking, config, reviews=[human_approval]
        )

        # Human approved → not stuck, regardless of age
        assert updated.state != PRTrackingState.ESCALATED
        assert 3 not in report.escalated

    async def test_bot_approval_does_not_count_as_human_signal(self) -> None:
        from datetime import timedelta

        pr = make_pr(
            number=4,
            user=User(login="copilot[bot]", id=1, type="Bot"),
            created_at=datetime.now(UTC) - timedelta(hours=48),
        )
        bot_approval = make_review(
            user=User(login="copilot-pull-request-reviewer[bot]", id=99, type="Bot"),
            state=ReviewState.APPROVED,
        )
        tracking = TrackedPR(number=4)
        config = make_config()
        config.stuck_age_hours = 24

        updated, report, _ = await self._run_process_pr(
            pr, tracking, config, reviews=[bot_approval]
        )
        assert updated.state == PRTrackingState.ESCALATED
        assert 4 in report.escalated

    async def test_already_escalated_pr_is_not_re_escalated(self) -> None:
        from datetime import timedelta

        pr = make_pr(
            number=5,
            user=User(login="copilot[bot]", id=1, type="Bot"),
            created_at=datetime.now(UTC) - timedelta(days=5),
        )
        tracking = TrackedPR(number=5, escalated=True, state=PRTrackingState.ESCALATED)
        config = make_config()
        config.stuck_age_hours = 24

        _updated, report, _ = await self._run_process_pr(pr, tracking, config)
        # report.escalated should NOT include this PR (no re-escalation)
        assert 5 not in report.escalated

    async def test_gate_disabled_when_stuck_age_hours_zero(self) -> None:
        from datetime import timedelta

        pr = make_pr(
            number=6,
            user=User(login="copilot[bot]", id=1, type="Bot"),
            created_at=datetime.now(UTC) - timedelta(days=30),
        )
        tracking = TrackedPR(number=6)
        config = make_config()
        config.stuck_age_hours = 0  # disabled

        _updated, report, _ = await self._run_process_pr(pr, tracking, config)
        assert 6 not in report.escalated


@pytest.mark.asyncio
class TestRetryWindowHours:
    """Sprint 3 E3: when the prior copilot attempt is older than
    retry_window_hours, copilot_attempts resets to 0 instead of escalating."""

    async def _run_handle_ci_fix(self, pr, tracking, config):
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
        mock_result = MagicMock()
        mock_result.comment_id = 99
        agent._copilot_bridge.request_ci_fix = AsyncMock(return_value=mock_result)
        agent._copilot_bridge._protocol._github = github
        report = PRAgentReport()
        updated = await agent._handle_ci_fix(pr, evaluation, tracking, report)
        return updated, report, agent

    async def test_old_attempt_resets_counter_instead_of_escalating(self) -> None:
        from datetime import timedelta

        copilot_user = User(login="copilot[bot]", id=1, type="Bot")
        pr = make_pr(number=10, user=copilot_user)
        tracking = TrackedPR(
            number=10,
            copilot_attempts=2,
            last_copilot_attempt_at=datetime.now(UTC) - timedelta(hours=48),
        )
        config = make_config(max_retries=2)
        config.copilot.retry_window_hours = 24

        updated, report, agent = await self._run_handle_ci_fix(pr, tracking, config)

        assert updated.state != PRTrackingState.ESCALATED
        assert 10 not in report.escalated
        assert 10 in report.fix_requested
        agent._copilot_bridge.request_ci_fix.assert_awaited_once()
        assert updated.copilot_attempts == 1

    async def test_recent_attempt_within_window_still_escalates(self) -> None:
        from datetime import timedelta

        copilot_user = User(login="copilot[bot]", id=1, type="Bot")
        pr = make_pr(number=11, user=copilot_user)
        tracking = TrackedPR(
            number=11,
            copilot_attempts=2,
            last_copilot_attempt_at=datetime.now(UTC) - timedelta(hours=2),
        )
        config = make_config(max_retries=2)
        config.copilot.retry_window_hours = 24

        updated, report, _agent = await self._run_handle_ci_fix(pr, tracking, config)
        assert updated.state == PRTrackingState.ESCALATED
        assert 11 in report.escalated

    async def test_window_zero_disables_reset(self) -> None:
        from datetime import timedelta

        copilot_user = User(login="copilot[bot]", id=1, type="Bot")
        pr = make_pr(number=12, user=copilot_user)
        tracking = TrackedPR(
            number=12,
            copilot_attempts=2,
            last_copilot_attempt_at=datetime.now(UTC) - timedelta(days=30),
        )
        config = make_config(max_retries=2)
        config.copilot.retry_window_hours = 0

        updated, report, _agent = await self._run_handle_ci_fix(pr, tracking, config)
        assert updated.state == PRTrackingState.ESCALATED
        assert 12 in report.escalated

    async def test_attempt_records_timestamp(self) -> None:
        copilot_user = User(login="copilot[bot]", id=1, type="Bot")
        pr = make_pr(number=13, user=copilot_user)
        tracking = TrackedPR(number=13)
        config = make_config(max_retries=2)

        updated, _report, _agent = await self._run_handle_ci_fix(pr, tracking, config)
        assert updated.last_copilot_attempt_at is not None
        delta = (datetime.now(UTC) - updated.last_copilot_attempt_at).total_seconds()
        assert 0 <= delta < 5


@pytest.mark.asyncio
class TestHandleReviewApprove:
    """Tests for _handle_review_approve idempotency, guards, and config gating.

    Pinning down the "multiple reviews per caretaker PR" symptom (PR #581 / v0.19.3):
    the auto-approve path must (a) refuse non-caretaker PRs, (b) skip duplicate
    approvals on the same head SHA, and (c) re-arm naturally when a new commit
    advances the SHA.
    """

    async def _run(
        self,
        pr,
        tracking: TrackedPR,
        *,
        auto_approve: bool = True,
        github=None,
    ):
        from caretaker.config import ReviewConfig
        from caretaker.pr_agent.agent import PRAgent, PRAgentReport

        config = make_config()
        config.review = ReviewConfig(auto_approve_caretaker_prs=auto_approve)
        github = github or AsyncMock()
        agent = PRAgent(github=github, owner="o", repo="r", config=config)
        report = PRAgentReport()
        updated = await agent._handle_review_approve(pr, tracking, report)
        return updated, report, github

    async def test_approves_caretaker_pr_first_time(self) -> None:
        pr = make_pr(number=1, head_ref="caretaker/x")
        pr.head_sha = "abc123"
        tracking = TrackedPR(number=1)

        updated, report, github = await self._run(pr, tracking)

        github.create_review.assert_awaited_once()
        assert updated.state == PRTrackingState.MERGE_READY
        assert updated.last_approved_sha == "abc123"
        assert 1 in report.approved
        assert report.errors == []

    async def test_idempotent_skip_on_same_sha(self) -> None:
        """Re-entry on the same head SHA must NOT submit a duplicate review."""
        pr = make_pr(number=2, head_ref="caretaker/x")
        pr.head_sha = "abc123"
        tracking = TrackedPR(number=2, last_approved_sha="abc123")

        updated, report, github = await self._run(pr, tracking)

        github.create_review.assert_not_awaited()
        assert updated.state == PRTrackingState.MERGE_READY
        assert updated.last_approved_sha == "abc123"
        assert 2 in report.approved

    async def test_new_sha_re_approves(self) -> None:
        """A new commit (different head SHA) re-arms the gate."""
        pr = make_pr(number=3, head_ref="caretaker/x")
        pr.head_sha = "newsha"
        tracking = TrackedPR(number=3, last_approved_sha="oldsha")

        updated, report, github = await self._run(pr, tracking)

        github.create_review.assert_awaited_once()
        assert updated.last_approved_sha == "newsha"
        assert 3 in report.approved

    async def test_refuses_non_caretaker_pr(self) -> None:
        """Defensive guard: never approve a non-caretaker PR even if routed here."""
        pr = make_pr(number=4, head_ref="dependabot/npm/foo-1.2.3")
        pr.head_sha = "abc123"
        tracking = TrackedPR(number=4)

        updated, report, github = await self._run(pr, tracking)

        github.create_review.assert_not_awaited()
        assert updated.state != PRTrackingState.MERGE_READY
        assert updated.last_approved_sha is None
        assert 4 in report.waiting

    async def test_approves_maintainer_bot_releases_json_pr(self) -> None:
        """F-7 regression: ``chore/releases-json-*`` is opened by the
        update-releases-json workflow after every release. The state
        machine routes it to ``request_review_approve`` (states.py:530),
        so the handler must accept it. Pre-fix it was refused on the
        ``is_caretaker_pr`` head-ref check, which is why every past
        v0.X.Y bump (#616, #618, ...) had to be merged manually."""
        pr = make_pr(number=8, head_ref="chore/releases-json-v0.25.0")
        pr.head_sha = "deadbeef"
        tracking = TrackedPR(number=8)

        updated, report, github = await self._run(pr, tracking)

        github.create_review.assert_awaited_once()
        assert updated.state == PRTrackingState.MERGE_READY
        assert updated.last_approved_sha == "deadbeef"
        assert 8 in report.approved

    async def test_disabled_flag_skips(self) -> None:
        pr = make_pr(number=5, head_ref="caretaker/x")
        pr.head_sha = "abc123"
        tracking = TrackedPR(number=5)

        updated, report, github = await self._run(pr, tracking, auto_approve=False)

        github.create_review.assert_not_awaited()
        assert updated.last_approved_sha is None
        assert 5 in report.waiting

    async def test_create_review_failure_records_error(self) -> None:
        pr = make_pr(number=6, head_ref="caretaker/x")
        pr.head_sha = "abc123"
        tracking = TrackedPR(number=6)
        github = AsyncMock()
        github.create_review.side_effect = RuntimeError("boom")

        updated, report, _ = await self._run(pr, tracking, github=github)

        assert updated.last_approved_sha is None
        assert any("auto-approve failed" in e for e in report.errors)
        assert 6 in report.waiting


@pytest.mark.asyncio
class TestHandleReviewClose:
    """Tests for _handle_review_close reason sanitization."""

    async def test_multiline_reason_collapses_to_single_line(self) -> None:
        from caretaker.pr_agent.agent import PRAgent, PRAgentReport

        pr = make_pr(number=11, head_ref="caretaker/x")
        tracking = TrackedPR(number=11)
        config = make_config()
        github = AsyncMock()
        agent = PRAgent(github=github, owner="o", repo="r", config=config)
        report = PRAgentReport()

        reason = "Infeasible / duplicate:\n\nThis already\nlanded in PR #999."
        await agent._handle_review_close(pr, tracking, report, reason)

        github.add_issue_comment.assert_awaited_once()
        # The blockquote line in the posted body should be a single line:
        body = github.add_issue_comment.await_args.args[3]
        assert "> Infeasible / duplicate: This already landed in PR #999." in body
        # No newline inside the blockquote:
        quoted_line = next(line for line in body.splitlines() if line.startswith("> "))
        assert "\n" not in quoted_line

    async def test_empty_reason_falls_back(self) -> None:
        from caretaker.pr_agent.agent import PRAgent, PRAgentReport

        pr = make_pr(number=12, head_ref="caretaker/x")
        tracking = TrackedPR(number=12)
        config = make_config()
        github = AsyncMock()
        agent = PRAgent(github=github, owner="o", repo="r", config=config)
        report = PRAgentReport()

        await agent._handle_review_close(pr, tracking, report, "   \n\n   ")
        body = github.add_issue_comment.await_args.args[3]
        assert "> no reason provided" in body


@pytest.mark.asyncio
class TestPublishReadinessCheckAppId:
    """Tests for proactive app_id ownership check in _publish_readiness_check.

    GitHub returns 403 "Invalid app_id" when a check run update is attempted by
    a different GitHub App than the one that created it.  The fix (v0.19.4 / #585)
    adds a proactive comparison so we skip the update and create a new check run
    *before* hitting the API error.
    """

    def _make_agent(self, github, *, app_id: int | None = None):
        from caretaker.pr_agent.agent import PRAgent

        config = make_config()
        config.readiness.enabled = True
        return PRAgent(
            github=github,
            owner="o",
            repo="r",
            config=config,
            app_id=app_id,
        )

    def _make_evaluation(self, pr):
        from caretaker.pr_agent.states import (
            CIEvaluation,
            CIStatus,
            PRStateEvaluation,
            ReadinessEvaluation,
            ReviewEvaluation,
        )

        readiness = ReadinessEvaluation(
            score=0.8,
            blockers=[],
            summary="Ready",
            conclusion="success",
        )
        ci = CIEvaluation(
            status=CIStatus.PASSING,
            failed_runs=[],
            pending_runs=[],
            passed_runs=[],
            action_required_runs=[],
            all_completed=True,
        )
        reviews = ReviewEvaluation(
            approved=False,
            changes_requested=False,
            pending=False,
            approving_reviews=[],
            blocking_reviews=[],
        )
        return PRStateEvaluation(
            pr=pr,
            ci=ci,
            reviews=reviews,
            readiness=readiness,
            readiness_verdict=None,
        )

    async def test_creates_new_check_when_owned_by_different_app(self) -> None:
        """When existing check_run.app_id != our app_id, skip update and create new."""
        github = AsyncMock()
        # Existing check was created by App 999, we are App 42
        github.find_check_run = AsyncMock(
            return_value=make_check_run(
                name="caretaker/pr-readiness",
                app_id=999,
            )
        )
        github.create_check_run = AsyncMock(return_value={"id": 77})
        github.update_check_run = AsyncMock()

        agent = self._make_agent(github, app_id=42)
        pr = make_pr(number=10, head_ref="caretaker/x")
        pr.head_sha = "deadbeef"
        tracking = TrackedPR(number=10)
        evaluation = self._make_evaluation(pr)

        await agent._publish_readiness_check(pr, tracking, evaluation)

        # Must NOT have attempted update (would 403)
        github.update_check_run.assert_not_awaited()
        # Must have created a new check run instead
        github.create_check_run.assert_awaited_once()

    async def test_updates_check_when_owned_by_same_app(self) -> None:
        """When existing check_run.app_id == our app_id, update the existing check."""
        github = AsyncMock()
        github.find_check_run = AsyncMock(
            return_value=make_check_run(
                name="caretaker/pr-readiness",
                app_id=42,
            )
        )
        github.update_check_run = AsyncMock()
        github.create_check_run = AsyncMock()

        agent = self._make_agent(github, app_id=42)
        pr = make_pr(number=11, head_ref="caretaker/x")
        pr.head_sha = "deadbeef"
        tracking = TrackedPR(number=11)
        evaluation = self._make_evaluation(pr)

        await agent._publish_readiness_check(pr, tracking, evaluation)

        # Must have updated (not created) since we own the check
        github.update_check_run.assert_awaited_once()
        github.create_check_run.assert_not_awaited()

    async def test_attempts_update_when_app_id_unknown(self) -> None:
        """When self._app_id is None (identity unknown), attempt update as before."""
        github = AsyncMock()
        # Existing check has no app_id metadata
        github.find_check_run = AsyncMock(
            return_value=make_check_run(
                name="caretaker/pr-readiness",
                app_id=None,
            )
        )
        github.update_check_run = AsyncMock()
        github.create_check_run = AsyncMock()

        # No app_id passed — identity unknown
        agent = self._make_agent(github, app_id=None)
        pr = make_pr(number=12, head_ref="caretaker/x")
        pr.head_sha = "deadbeef"
        tracking = TrackedPR(number=12)
        evaluation = self._make_evaluation(pr)

        await agent._publish_readiness_check(pr, tracking, evaluation)

        # With unknown identity, we attempt update (best effort)
        github.update_check_run.assert_awaited_once()
        github.create_check_run.assert_not_awaited()

    async def test_creates_new_check_when_no_existing_check(self) -> None:
        """When there is no existing check run at all, create a new one."""
        github = AsyncMock()
        github.find_check_run = AsyncMock(return_value=None)
        github.create_check_run = AsyncMock(return_value={"id": 55})
        github.update_check_run = AsyncMock()

        agent = self._make_agent(github, app_id=42)
        pr = make_pr(number=13, head_ref="caretaker/x")
        pr.head_sha = "deadbeef"
        tracking = TrackedPR(number=13)
        evaluation = self._make_evaluation(pr)

        await agent._publish_readiness_check(pr, tracking, evaluation)

        github.create_check_run.assert_awaited_once()
        github.update_check_run.assert_not_awaited()


@pytest.mark.asyncio
class TestAdvisoryModeNeutralConclusion:
    """Advisory mode must publish 'neutral' instead of 'failure' so that
    the caretaker/pr-readiness check never blocks branch protection even
    when an operator has listed it as a required check.

    gate_only / gate_and_merge modes must still publish 'failure' to
    actively gate merges.
    """

    def _make_agent(self, github, *, mode: MergeAuthorityMode = MergeAuthorityMode.ADVISORY):
        from caretaker.pr_agent.agent import PRAgent

        config = make_config()
        config.readiness.enabled = True
        config.merge_authority = MergeAuthorityConfig(mode=mode)
        return PRAgent(github=github, owner="o", repo="r", config=config)

    def _make_failing_evaluation(self, pr):
        from caretaker.pr_agent.states import (
            CIEvaluation,
            CIStatus,
            PRStateEvaluation,
            ReadinessEvaluation,
            ReviewEvaluation,
        )

        readiness = ReadinessEvaluation(
            score=0.2,
            blockers=["ci_failing"],
            summary="CI failing",
            conclusion="failure",
        )
        ci = CIEvaluation(
            status=CIStatus.FAILING,
            failed_runs=[],
            pending_runs=[],
            passed_runs=[],
            action_required_runs=[],
            all_completed=True,
        )
        reviews = ReviewEvaluation(
            approved=False,
            changes_requested=False,
            pending=False,
            approving_reviews=[],
            blocking_reviews=[],
        )
        return PRStateEvaluation(
            pr=pr,
            ci=ci,
            reviews=reviews,
            readiness=readiness,
            readiness_verdict=None,
        )

    async def test_advisory_mode_publishes_neutral_not_failure(self) -> None:
        """Default advisory mode: a blocked PR must never publish 'failure'."""
        github = AsyncMock()
        github.find_check_run = AsyncMock(return_value=None)
        github.create_check_run = AsyncMock(return_value={"id": 1})

        agent = self._make_agent(github, mode=MergeAuthorityMode.ADVISORY)
        pr = make_pr(number=1, head_ref="fix/something")
        pr.head_sha = "abc123"
        tracking = TrackedPR(number=1)
        evaluation = self._make_failing_evaluation(pr)

        await agent._publish_readiness_check(pr, tracking, evaluation)

        github.create_check_run.assert_awaited_once()
        conclusion_arg = github.create_check_run.call_args.kwargs["conclusion"]
        assert conclusion_arg == "neutral", (
            f"advisory mode must publish 'neutral', got '{conclusion_arg}'"
        )

    async def test_gate_only_mode_publishes_failure(self) -> None:
        """gate_only mode must publish 'failure' to block branch protection."""
        github = AsyncMock()
        github.find_check_run = AsyncMock(return_value=None)
        github.create_check_run = AsyncMock(return_value={"id": 2})

        agent = self._make_agent(github, mode=MergeAuthorityMode.GATE_ONLY)
        pr = make_pr(number=2, head_ref="fix/something")
        pr.head_sha = "def456"
        tracking = TrackedPR(number=2)
        evaluation = self._make_failing_evaluation(pr)

        await agent._publish_readiness_check(pr, tracking, evaluation)

        github.create_check_run.assert_awaited_once()
        conclusion_arg = github.create_check_run.call_args.kwargs["conclusion"]
        assert conclusion_arg == "failure", (
            f"gate_only mode must publish 'failure', got '{conclusion_arg}'"
        )

    async def test_gate_and_merge_mode_publishes_failure(self) -> None:
        """gate_and_merge mode must publish 'failure' to block branch protection."""
        github = AsyncMock()
        github.find_check_run = AsyncMock(return_value=None)
        github.create_check_run = AsyncMock(return_value={"id": 3})

        agent = self._make_agent(github, mode=MergeAuthorityMode.GATE_AND_MERGE)
        pr = make_pr(number=3, head_ref="fix/something")
        pr.head_sha = "ghi789"
        tracking = TrackedPR(number=3)
        evaluation = self._make_failing_evaluation(pr)

        await agent._publish_readiness_check(pr, tracking, evaluation)

        github.create_check_run.assert_awaited_once()
        conclusion_arg = github.create_check_run.call_args.kwargs["conclusion"]
        assert conclusion_arg == "failure", (
            f"gate_and_merge mode must publish 'failure', got '{conclusion_arg}'"
        )

    async def test_advisory_mode_success_still_publishes_success(self) -> None:
        """Advisory mode must not change 'success' conclusions."""
        from caretaker.pr_agent.states import (
            CIEvaluation,
            CIStatus,
            PRStateEvaluation,
            ReadinessEvaluation,
            ReviewEvaluation,
        )

        github = AsyncMock()
        github.find_check_run = AsyncMock(return_value=None)
        github.create_check_run = AsyncMock(return_value={"id": 4})

        agent = self._make_agent(github, mode=MergeAuthorityMode.ADVISORY)
        pr = make_pr(number=4, head_ref="fix/something")
        pr.head_sha = "jkl012"
        tracking = TrackedPR(number=4)

        readiness = ReadinessEvaluation(
            score=1.0,
            blockers=[],
            summary="Ready",
            conclusion="success",
        )
        ci = CIEvaluation(
            status=CIStatus.PASSING,
            failed_runs=[],
            pending_runs=[],
            passed_runs=[],
            action_required_runs=[],
            all_completed=True,
        )
        reviews = ReviewEvaluation(
            approved=True,
            changes_requested=False,
            pending=False,
            approving_reviews=[],
            blocking_reviews=[],
        )
        evaluation = PRStateEvaluation(
            pr=pr, ci=ci, reviews=reviews, readiness=readiness, readiness_verdict=None
        )

        await agent._publish_readiness_check(pr, tracking, evaluation)

        github.create_check_run.assert_awaited_once()
        conclusion_arg = github.create_check_run.call_args.kwargs["conclusion"]
        assert conclusion_arg == "success"


class TestTerminalReadinessOnCloseMerge:
    """Once a PR is merged or closed, ``caretaker/pr-readiness`` must transition
    to a terminal conclusion regardless of any in-progress blockers in the
    evaluation. PR #609 was the motivating incident — the check stayed
    in_progress for hours after the human merged it.
    """

    def _agent(self, github):
        from caretaker.pr_agent.agent import PRAgent

        config = make_config()
        config.readiness.enabled = True
        return PRAgent(github=github, owner="o", repo="r", config=config)

    def _in_progress_eval(self, pr):
        from caretaker.pr_agent.states import (
            CIEvaluation,
            CIStatus,
            PRStateEvaluation,
            ReadinessEvaluation,
            ReviewEvaluation,
        )

        return PRStateEvaluation(
            pr=pr,
            ci=CIEvaluation(
                status=CIStatus.PENDING,
                failed_runs=[],
                pending_runs=[],
                passed_runs=[],
                action_required_runs=[],
                all_completed=False,
            ),
            reviews=ReviewEvaluation(
                approved=False,
                changes_requested=False,
                pending=True,
                approving_reviews=[],
                blocking_reviews=[],
            ),
            readiness=ReadinessEvaluation(
                score=0.3,
                blockers=["ci_pending", "required_review_missing"],
                summary="In progress",
                conclusion="in_progress",
            ),
        )

    async def test_merged_pr_publishes_success_even_with_pending_blockers(self) -> None:
        github = AsyncMock()
        github.find_check_run = AsyncMock(return_value=None)
        github.create_check_run = AsyncMock(return_value={"id": 1})

        agent = self._agent(github)
        pr = make_pr(number=609, head_ref="feat/x", merged=True, state=PRState.CLOSED)
        pr.head_sha = "abc123"
        from datetime import UTC, datetime

        pr.merged_at = datetime(2026, 4, 26, 21, 30, tzinfo=UTC)
        tracking = TrackedPR(number=609)

        await agent._publish_readiness_check(pr, tracking, self._in_progress_eval(pr))

        github.create_check_run.assert_awaited_once()
        kwargs = github.create_check_run.call_args.kwargs
        assert kwargs["conclusion"] == "success"
        assert kwargs["status"] == "completed"

    async def test_closed_unmerged_pr_publishes_neutral(self) -> None:
        github = AsyncMock()
        github.find_check_run = AsyncMock(return_value=None)
        github.create_check_run = AsyncMock(return_value={"id": 2})

        agent = self._agent(github)
        pr = make_pr(number=42, head_ref="feat/x", merged=False, state=PRState.CLOSED)
        pr.head_sha = "abc123"
        tracking = TrackedPR(number=42)

        await agent._publish_readiness_check(pr, tracking, self._in_progress_eval(pr))

        github.create_check_run.assert_awaited_once()
        kwargs = github.create_check_run.call_args.kwargs
        assert kwargs["conclusion"] == "neutral"
        assert kwargs["status"] == "completed"

    async def test_run_finalizes_readiness_check_for_externally_merged_pr(self) -> None:
        """run() with pr_number for a PR that's been closed/merged externally
        must still finalize the readiness check (one-shot, idempotent via
        ``readiness_check_finalized``).
        """
        from datetime import UTC, datetime

        github = AsyncMock()
        merged_pr = make_pr(number=609, head_ref="feat/x", merged=True, state=PRState.CLOSED)
        merged_pr.head_sha = "abc123"
        merged_pr.merged_at = datetime(2026, 4, 26, 21, 30, tzinfo=UTC)
        github.get_pull_request = AsyncMock(return_value=merged_pr)
        github.find_check_run = AsyncMock(return_value=None)
        github.create_check_run = AsyncMock(return_value={"id": 5})

        agent = self._agent(github)
        tracking = TrackedPR(number=609)
        tracked = {609: tracking}

        report, tracked = await agent.run(tracked, pr_number=609)

        # Finalization happened
        assert tracked[609].readiness_check_finalized is True
        github.create_check_run.assert_awaited_once()
        assert github.create_check_run.call_args.kwargs["conclusion"] == "success"

        # Second run() with same closed PR is a no-op (one-shot)
        github.create_check_run.reset_mock()
        report2, tracked2 = await agent.run(tracked, pr_number=609)
        github.create_check_run.assert_not_awaited()

    async def test_run_finalizes_readiness_check_for_untracked_merged_pr(self) -> None:
        """run() with pr_number for a merged PR that was never tracked
        (state save lost between open and merge, or the PR closed before
        the first agent run could persist tracking) must still finalize
        the readiness check — otherwise it dangles ``in_progress`` forever.

        Live incident: caretaker-qa#79. Opened 2026-04-28T08:53Z, merged
        2026-04-28T23:18Z. Never appeared in ``tracked_prs`` (the state
        snapshot at 00:15Z next day had 36 entries, none for #79). The
        ``caretaker/pr-readiness`` check at the head SHA stayed
        ``in_progress`` with no terminal conclusion because the merge-time
        finalize gate at ``_run`` line 228 was guarded by
        ``tracking is not None and not tracking.readiness_check_finalized``.
        """
        from datetime import UTC, datetime

        github = AsyncMock()
        merged_pr = make_pr(
            number=79,
            head_ref="copilot/upgrade",
            merged=True,
            state=PRState.CLOSED,
        )
        merged_pr.head_sha = "b23447de"
        merged_pr.merged_at = datetime(2026, 4, 28, 23, 18, tzinfo=UTC)
        github.get_pull_request = AsyncMock(return_value=merged_pr)
        github.find_check_run = AsyncMock(return_value=None)
        github.create_check_run = AsyncMock(return_value={"id": 7})

        agent = self._agent(github)
        # Critical: tracked_prs is EMPTY — this PR was never tracked.
        tracked: dict[int, TrackedPR] = {}

        report, tracked = await agent.run(tracked, pr_number=79)

        # Finalization must happen even though tracking didn't exist on entry.
        github.create_check_run.assert_awaited_once()
        assert github.create_check_run.call_args.kwargs["conclusion"] == "success"
        # A tracking entry should have been lazily created and marked finalized
        # so a subsequent run() is a no-op (idempotency preserved).
        assert 79 in tracked
        assert tracked[79].readiness_check_finalized is True

        # Replay the merge event: must NOT re-publish the check.
        github.create_check_run.reset_mock()
        await agent.run(tracked, pr_number=79)
        github.create_check_run.assert_not_awaited()


class TestTerminalReadinessOnEscalation:
    """An escalated PR has been handed off to humans — the readiness
    check must transition to a terminal conclusion so it stops dangling
    in ``in_progress`` while the PR sits awaiting human review.

    Motivating incident: PR #613. Caretaker scored the PR at 30%
    immediately after open (CI hadn't started), escalated within 45s,
    released ownership, and the ``caretaker/pr-readiness`` GitHub Check
    stayed ``in_progress`` indefinitely even after CI passed and a
    review posted — because ``required_review_missing`` kept the
    readiness conclusion at ``in_progress`` and there was no terminal
    branch for escalated-but-still-open PRs.
    """

    def _agent(self, github, mode: MergeAuthorityMode = MergeAuthorityMode.ADVISORY):
        from caretaker.pr_agent.agent import PRAgent

        config = make_config()
        config.readiness.enabled = True
        config.merge_authority.mode = mode
        return PRAgent(github=github, owner="o", repo="r", config=config)

    def _in_progress_eval(self, pr, *, conclusion: str = "in_progress"):
        from caretaker.pr_agent.states import (
            CIEvaluation,
            CIStatus,
            PRStateEvaluation,
            ReadinessEvaluation,
            ReviewEvaluation,
        )

        return PRStateEvaluation(
            pr=pr,
            ci=CIEvaluation(
                status=CIStatus.PASSING,
                failed_runs=[],
                pending_runs=[],
                passed_runs=[],
                action_required_runs=[],
                all_completed=True,
            ),
            reviews=ReviewEvaluation(
                approved=False,
                changes_requested=False,
                pending=True,
                approving_reviews=[],
                blocking_reviews=[],
            ),
            readiness=ReadinessEvaluation(
                score=0.5,
                blockers=["required_review_missing"],
                summary="Awaiting human review",
                conclusion=conclusion,
            ),
        )

    async def test_escalated_open_pr_in_advisory_mode_publishes_neutral(self) -> None:
        """Advisory mode (the default) publishes ``neutral`` so the check
        is informational and won't block any branch-protection rule even
        if an operator has listed it as required."""
        github = AsyncMock()
        github.find_check_run = AsyncMock(return_value=None)
        github.create_check_run = AsyncMock(return_value={"id": 1})

        agent = self._agent(github)
        pr = make_pr(number=613, head_ref="feat/byoca", state=PRState.OPEN)
        pr.head_sha = "abc123"
        tracking = TrackedPR(number=613, escalated=True, state=PRTrackingState.ESCALATED)

        await agent._publish_readiness_check(pr, tracking, self._in_progress_eval(pr))

        github.create_check_run.assert_awaited_once()
        kwargs = github.create_check_run.call_args.kwargs
        assert kwargs["conclusion"] == "neutral"
        assert kwargs["status"] == "completed"

    async def test_escalated_open_pr_in_gate_mode_publishes_failure(self) -> None:
        """Gate modes publish ``failure`` so branch protection still blocks
        the merge — operators who've opted into gating want the check to
        stay enforcing, not advisory, even after escalation."""
        github = AsyncMock()
        github.find_check_run = AsyncMock(return_value=None)
        github.create_check_run = AsyncMock(return_value={"id": 2})

        agent = self._agent(github, mode=MergeAuthorityMode.GATE_ONLY)
        pr = make_pr(number=613, head_ref="feat/byoca", state=PRState.OPEN)
        pr.head_sha = "abc123"
        tracking = TrackedPR(number=613, escalated=True, state=PRTrackingState.ESCALATED)

        await agent._publish_readiness_check(pr, tracking, self._in_progress_eval(pr))

        github.create_check_run.assert_awaited_once()
        kwargs = github.create_check_run.call_args.kwargs
        assert kwargs["conclusion"] == "failure"
        assert kwargs["status"] == "completed"

    async def test_escalated_pr_with_success_conclusion_still_publishes_success(self) -> None:
        """If a human approves and CI is green after caretaker had
        escalated, the check must reflect that the PR is now ready —
        ``conclusion == "success"`` wins over the escalated terminal
        branch so the PR isn't permanently locked at neutral/failure.
        """
        github = AsyncMock()
        github.find_check_run = AsyncMock(return_value=None)
        github.create_check_run = AsyncMock(return_value={"id": 3})

        agent = self._agent(github)
        pr = make_pr(number=613, head_ref="feat/byoca", state=PRState.OPEN)
        pr.head_sha = "abc123"
        tracking = TrackedPR(number=613, escalated=True, state=PRTrackingState.ESCALATED)

        await agent._publish_readiness_check(
            pr, tracking, self._in_progress_eval(pr, conclusion="success")
        )

        github.create_check_run.assert_awaited_once()
        kwargs = github.create_check_run.call_args.kwargs
        assert kwargs["conclusion"] == "success"
        assert kwargs["status"] == "completed"

    async def test_escalated_pr_recognized_via_state_field_alone(self) -> None:
        """``tracking.state == ESCALATED`` should trigger the terminal
        branch even if the legacy ``escalated`` boolean drifted out of
        sync (e.g. older persisted tracking that didn't set both)."""
        github = AsyncMock()
        github.find_check_run = AsyncMock(return_value=None)
        github.create_check_run = AsyncMock(return_value={"id": 4})

        agent = self._agent(github)
        pr = make_pr(number=613, head_ref="feat/byoca", state=PRState.OPEN)
        pr.head_sha = "abc123"
        # Only the state field set; ``escalated`` boolean stays False.
        tracking = TrackedPR(number=613, escalated=False, state=PRTrackingState.ESCALATED)

        await agent._publish_readiness_check(pr, tracking, self._in_progress_eval(pr))

        github.create_check_run.assert_awaited_once()
        kwargs = github.create_check_run.call_args.kwargs
        assert kwargs["conclusion"] == "neutral"

    async def test_non_escalated_in_progress_pr_stays_in_progress(self) -> None:
        """Regression guard: a non-escalated open PR with in-progress
        readiness must continue to publish ``in_progress`` (the existing
        contract — escalation alone is what flips us to terminal)."""
        github = AsyncMock()
        github.find_check_run = AsyncMock(return_value=None)
        github.create_check_run = AsyncMock(return_value={"id": 5})

        agent = self._agent(github)
        pr = make_pr(number=42, head_ref="feat/x", state=PRState.OPEN)
        pr.head_sha = "abc123"
        tracking = TrackedPR(number=42, escalated=False, state=PRTrackingState.DISCOVERED)

        await agent._publish_readiness_check(pr, tracking, self._in_progress_eval(pr))

        github.create_check_run.assert_awaited_once()
        kwargs = github.create_check_run.call_args.kwargs
        assert kwargs["conclusion"] is None
        assert kwargs["status"] == "in_progress"


class TestResyncOpenPRs:
    """The resync method is the polling fallback for dropped webhooks. It
    must call _process_pr for each open PR and update tracking.
    """

    async def test_resync_processes_each_open_pr(self) -> None:
        from caretaker.pr_agent.agent import PRAgent

        pr1 = make_pr(number=1, head_ref="feat/a")
        pr2 = make_pr(number=2, head_ref="feat/b")

        github = AsyncMock()
        github.list_pull_requests = AsyncMock(return_value=[pr1, pr2])

        config = make_config()
        agent = PRAgent(github=github, owner="o", repo="r", config=config)

        # Stub _process_pr so we don't have to wire every downstream call
        seen: list[int] = []

        async def fake_process(pr, tracking, report):
            seen.append(pr.number)
            tracking.readiness_score = 1.0
            return tracking

        agent._process_pr = fake_process  # type: ignore[method-assign]

        tracked: dict = {}
        report, tracked = await agent.resync_open_prs(tracked)

        assert sorted(seen) == [1, 2]
        assert report.monitored == 2
        assert tracked[1].readiness_score == 1.0
        assert tracked[2].readiness_score == 1.0
        assert tracked[1].last_checked is not None

    async def test_resync_continues_after_per_pr_error(self) -> None:
        from caretaker.pr_agent.agent import PRAgent

        pr1 = make_pr(number=1, head_ref="feat/a")
        pr2 = make_pr(number=2, head_ref="feat/b")

        github = AsyncMock()
        github.list_pull_requests = AsyncMock(return_value=[pr1, pr2])

        config = make_config()
        agent = PRAgent(github=github, owner="o", repo="r", config=config)

        seen: list[int] = []

        async def fake_process(pr, tracking, report):
            seen.append(pr.number)
            if pr.number == 1:
                raise RuntimeError("simulated transient")
            tracking.readiness_score = 1.0
            return tracking

        agent._process_pr = fake_process  # type: ignore[method-assign]

        report, tracked = await agent.resync_open_prs({})

        assert sorted(seen) == [1, 2]  # both attempted
        assert any("PR #1" in e for e in report.errors)
        assert tracked[2].readiness_score == 1.0  # PR #2 still processed
