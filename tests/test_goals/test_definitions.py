"""Tests for concrete goal definitions."""

from __future__ import annotations

from unittest.mock import AsyncMock

from caretaker.config import MaintainerConfig
from caretaker.goals.definitions import (
    CIHealthGoal,
    DocumentationCurrencyGoal,
    IssueTriageGoal,
    PRLifecycleGoal,
    SecurityPostureGoal,
    SelfHealthGoal,
    UpgradeCurrencyGoal,
    build_goals,
)
from caretaker.goals.engine import GoalContext
from caretaker.state.models import (
    IssueTrackingState,
    OrchestratorState,
    PRTrackingState,
    RunSummary,
    TrackedIssue,
    TrackedPR,
)


def make_context(summary: RunSummary | None = None) -> GoalContext:
    return GoalContext(
        github=AsyncMock(),
        owner="o",
        repo="r",
        config=MaintainerConfig(),
        current_summary=summary,
    )


class TestGoalDefinitions:
    async def test_ci_health_goal_scores_pr_and_branch_health(self) -> None:
        state = OrchestratorState(
            tracked_prs={
                1: TrackedPR(number=1, state=PRTrackingState.CI_PASSING),
                2: TrackedPR(number=2, state=PRTrackingState.CI_FAILING),
            },
            reported_build_sigs=["sig1", "sig2"],
        )

        snapshot = await CIHealthGoal().evaluate(state, make_context())

        # pr_ci_score=0.5, branch_score=0.8 => 0.5*0.6 + 0.8*0.4 = 0.62
        assert snapshot.score == 0.62
        assert snapshot.details["open_prs"] == 2
        assert snapshot.details["prs_ci_passing"] == 1

    async def test_pr_lifecycle_goal_full_score_when_no_open_prs(self) -> None:
        state = OrchestratorState(
            tracked_prs={
                1: TrackedPR(number=1, state=PRTrackingState.MERGED),
            }
        )

        snapshot = await PRLifecycleGoal().evaluate(state, make_context())

        assert snapshot.score == 1.0
        assert snapshot.details["open_prs"] == 0

    async def test_issue_triage_goal_tracks_untriaged_and_assigned(self) -> None:
        state = OrchestratorState(
            tracked_issues={
                1: TrackedIssue(number=1, state=IssueTrackingState.NEW),
                2: TrackedIssue(number=2, state=IssueTrackingState.ASSIGNED),
            }
        )

        snapshot = await IssueTriageGoal().evaluate(state, make_context())

        assert snapshot.score == 0.25
        assert snapshot.details["open_issues"] == 2
        assert snapshot.details["untriaged"] == 1
        assert snapshot.details["assigned"] == 1

    async def test_security_posture_goal_uses_addressed_ratio(self) -> None:
        summary = RunSummary(
            security_findings_found=5,
            security_issues_created=2,
            security_false_positives=1,
        )

        snapshot = await SecurityPostureGoal().evaluate(OrchestratorState(), make_context(summary))

        # addressed=3, unaddressed=2 => 1 - 2/5 = 0.6
        assert snapshot.score == 0.6
        assert snapshot.details["unaddressed"] == 2

    async def test_upgrade_currency_goal_penalizes_major_dependency_issues(self) -> None:
        summary = RunSummary(upgrade_available=False, dependency_major_issues=2)

        snapshot = await UpgradeCurrencyGoal().evaluate(OrchestratorState(), make_context(summary))

        assert snapshot.score == 0.6
        assert snapshot.details["major_dep_issues"] == 2

    async def test_self_health_goal_accounts_for_repeated_errors(self) -> None:
        summary = RunSummary(errors=["e1"], self_heal_local_issues=1)
        state = OrchestratorState(
            run_history=[
                RunSummary(errors=["e1", "x"]),
                RunSummary(errors=["e1", "y"]),
                RunSummary(errors=["e1", "z"]),
            ]
        )

        snapshot = await SelfHealthGoal().evaluate(state, make_context(summary))

        assert snapshot.score == 0.785
        assert snapshot.details["repeated_errors"] == 1

    async def test_documentation_goal_reflects_open_docs_pr(self) -> None:
        summary = RunSummary(docs_prs_analyzed=3, docs_pr_opened=123)

        snapshot = await DocumentationCurrencyGoal().evaluate(
            OrchestratorState(), make_context(summary)
        )

        assert snapshot.score == 0.8
        assert snapshot.details["doc_pr_opened"] == 123

    def test_build_goals_constructs_expected_goal_set(self) -> None:
        goals = build_goals()

        goal_ids = {goal.goal_id for goal in goals}
        assert len(goals) == 7
        assert goal_ids == {
            "ci_health",
            "pr_lifecycle",
            "issue_triage",
            "security_posture",
            "upgrade_currency",
            "self_health",
            "documentation",
        }
