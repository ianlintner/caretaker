"""Concrete goal definitions for repository caretaking.

Each goal implements a quantitative scoring function (0.0–1.0) that
the :class:`~caretaker.goals.engine.GoalEngine` uses to prioritise
agent dispatch and detect divergence.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from caretaker.goals.engine import Goal, GoalContext
from caretaker.goals.models import GoalSnapshot
from caretaker.state.models import IssueTrackingState, PRTrackingState

if TYPE_CHECKING:
    from caretaker.state.models import OrchestratorState


_TERMINAL_PR_STATES = frozenset(
    {
        PRTrackingState.MERGED,
        PRTrackingState.CLOSED,
        PRTrackingState.ESCALATED,
    }
)

_CI_OK_PR_STATES = frozenset(
    {
        PRTrackingState.CI_PASSING,
        PRTrackingState.REVIEW_PENDING,
        PRTrackingState.REVIEW_APPROVED,
        PRTrackingState.MERGE_READY,
    }
)

_TERMINAL_ISSUE_STATES = frozenset(
    {
        IssueTrackingState.COMPLETED,
        IssueTrackingState.CLOSED,
        IssueTrackingState.ESCALATED,
    }
)

# Progress weights for PR lifecycle states (higher = closer to terminal)
_PR_PROGRESS: dict[PRTrackingState, float] = {
    PRTrackingState.DISCOVERED: 0.10,
    PRTrackingState.CI_PENDING: 0.20,
    PRTrackingState.CI_FAILING: 0.15,
    PRTrackingState.CI_PASSING: 0.50,
    PRTrackingState.REVIEW_PENDING: 0.60,
    PRTrackingState.REVIEW_CHANGES_REQUESTED: 0.40,
    PRTrackingState.REVIEW_APPROVED: 0.80,
    PRTrackingState.FIX_REQUESTED: 0.30,
    PRTrackingState.FIX_IN_PROGRESS: 0.35,
    PRTrackingState.MERGE_READY: 0.95,
}

# Progress weights for issue lifecycle states
_ISSUE_PROGRESS: dict[IssueTrackingState, float] = {
    IssueTrackingState.NEW: 0.00,
    IssueTrackingState.TRIAGED: 0.30,
    IssueTrackingState.ASSIGNED: 0.50,
    IssueTrackingState.IN_PROGRESS: 0.70,
    IssueTrackingState.PR_OPENED: 0.90,
    IssueTrackingState.STALE: 0.10,
}


# ── G1: CI Health ────────────────────────────────────────────────


class CIHealthGoal(Goal):
    """Default-branch and PR CI should be green."""

    @property
    def goal_id(self) -> str:
        return "ci_health"

    @property
    def description(self) -> str:
        return "All CI pipelines passing on default branch and open PRs"

    @property
    def contributing_agents(self) -> list[str]:
        return ["pr", "devops"]

    @property
    def priority(self) -> float:
        return 2.0

    async def evaluate(
        self,
        state: OrchestratorState,
        context: GoalContext,
    ) -> GoalSnapshot:
        open_prs = {
            n: p for n, p in state.tracked_prs.items() if p.state not in _TERMINAL_PR_STATES
        }

        if open_prs:
            passing = sum(1 for p in open_prs.values() if p.state in _CI_OK_PR_STATES)
            pr_ci_score = passing / len(open_prs)
        else:
            pr_ci_score = 1.0

        recent_sigs = len(state.reported_build_sigs)
        branch_score = max(0.0, 1.0 - (recent_sigs * 0.1))

        score = round(max(0.0, min(1.0, pr_ci_score * 0.6 + branch_score * 0.4)), 3)

        return GoalSnapshot(
            goal_id=self.goal_id,
            score=score,
            details={
                "open_prs": len(open_prs),
                "prs_ci_passing": passing if open_prs else 0,
                "pr_ci_score": round(pr_ci_score, 3),
                "unresolved_build_sigs": recent_sigs,
                "branch_score": round(branch_score, 3),
            },
        )


# ── G2: PR Lifecycle ────────────────────────────────────────────


class PRLifecycleGoal(Goal):
    """Every PR should reach a terminal state within SLA."""

    @property
    def goal_id(self) -> str:
        return "pr_lifecycle"

    @property
    def description(self) -> str:
        return "All PRs reviewed, builds passing, merged or properly closed"

    @property
    def contributing_agents(self) -> list[str]:
        return ["pr", "stale", "escalation"]

    @property
    def priority(self) -> float:
        return 1.5

    async def evaluate(
        self,
        state: OrchestratorState,
        context: GoalContext,
    ) -> GoalSnapshot:
        open_prs = {
            n: p for n, p in state.tracked_prs.items() if p.state not in _TERMINAL_PR_STATES
        }

        if not open_prs:
            return GoalSnapshot(goal_id=self.goal_id, score=1.0, details={"open_prs": 0})

        progress_scores = [_PR_PROGRESS.get(p.state, 0.1) for p in open_prs.values()]
        score = round(sum(progress_scores) / len(progress_scores), 3)

        state_dist = {}
        for s in PRTrackingState:
            count = sum(1 for p in open_prs.values() if p.state == s)
            if count > 0:
                state_dist[s.value] = count

        return GoalSnapshot(
            goal_id=self.goal_id,
            score=score,
            details={"open_prs": len(open_prs), "state_distribution": state_dist},
        )


# ── G3: Issue Triage ────────────────────────────────────────────


class IssueTriageGoal(Goal):
    """All issues should be classified, assigned, and tracked."""

    @property
    def goal_id(self) -> str:
        return "issue_triage"

    @property
    def description(self) -> str:
        return "All issues classified, assigned to work, and tracked to resolution"

    @property
    def contributing_agents(self) -> list[str]:
        return ["issue", "charlie"]

    async def evaluate(
        self,
        state: OrchestratorState,
        context: GoalContext,
    ) -> GoalSnapshot:
        open_issues = {
            n: i for n, i in state.tracked_issues.items() if i.state not in _TERMINAL_ISSUE_STATES
        }

        if not open_issues:
            return GoalSnapshot(goal_id=self.goal_id, score=1.0, details={"open_issues": 0})

        progress = [_ISSUE_PROGRESS.get(i.state, 0.0) for i in open_issues.values()]
        score = round(sum(progress) / len(progress), 3)

        return GoalSnapshot(
            goal_id=self.goal_id,
            score=score,
            details={
                "open_issues": len(open_issues),
                "untriaged": sum(
                    1 for i in open_issues.values() if i.state == IssueTrackingState.NEW
                ),
                "assigned": sum(
                    1
                    for i in open_issues.values()
                    if i.state in (IssueTrackingState.ASSIGNED, IssueTrackingState.IN_PROGRESS)
                ),
            },
        )


# ── G4: Security Posture ────────────────────────────────────────


class SecurityPostureGoal(Goal):
    """Zero unaddressed security findings above severity threshold."""

    @property
    def goal_id(self) -> str:
        return "security_posture"

    @property
    def description(self) -> str:
        return "All security findings triaged and addressed"

    @property
    def contributing_agents(self) -> list[str]:
        return ["security", "deps"]

    @property
    def priority(self) -> float:
        return 2.0

    @property
    def critical_threshold(self) -> float:
        return 0.5

    async def evaluate(
        self,
        state: OrchestratorState,
        context: GoalContext,
    ) -> GoalSnapshot:
        summary = context.current_summary or state.last_run

        if summary is None:
            return GoalSnapshot(
                goal_id=self.goal_id,
                score=1.0,
                details={"note": "No run data available"},
            )

        findings = summary.security_findings_found
        addressed = summary.security_issues_created + summary.security_false_positives
        unaddressed = max(0, findings - addressed)

        score = 1.0 if findings == 0 else round(max(0.0, 1.0 - (unaddressed / max(findings, 1))), 3)

        return GoalSnapshot(
            goal_id=self.goal_id,
            score=score,
            details={
                "findings": findings,
                "addressed": addressed,
                "unaddressed": unaddressed,
            },
        )


# ── G5: Upgrade Currency ────────────────────────────────────────


class UpgradeCurrencyGoal(Goal):
    """Caretaker and dependencies should be on the latest stable versions."""

    @property
    def goal_id(self) -> str:
        return "upgrade_currency"

    @property
    def description(self) -> str:
        return "Caretaker and dependencies on latest stable versions"

    @property
    def contributing_agents(self) -> list[str]:
        return ["upgrade", "deps"]

    @property
    def priority(self) -> float:
        return 0.8

    @property
    def satisfaction_threshold(self) -> float:
        return 1.0

    async def evaluate(
        self,
        state: OrchestratorState,
        context: GoalContext,
    ) -> GoalSnapshot:
        summary = context.current_summary or state.last_run

        if summary is None:
            return GoalSnapshot(
                goal_id=self.goal_id,
                score=0.5,
                details={"note": "No run data — unknown upgrade status"},
            )

        upgrade_needed = summary.upgrade_available
        score = 0.0 if upgrade_needed else 1.0

        dep_pending = summary.dependency_major_issues
        if dep_pending > 0:
            score = max(0.0, score - 0.2 * dep_pending)

        return GoalSnapshot(
            goal_id=self.goal_id,
            score=round(max(0.0, min(1.0, score)), 3),
            details={
                "upgrade_needed": upgrade_needed,
                "target_version": summary.upgrade_version,
                "major_dep_issues": dep_pending,
            },
        )


# ── G6: Self Health ─────────────────────────────────────────────


class SelfHealthGoal(Goal):
    """Caretaker itself should run error-free and not get stuck in loops."""

    @property
    def goal_id(self) -> str:
        return "self_health"

    @property
    def description(self) -> str:
        return "Caretaker running error-free, detecting and escalating when stuck"

    @property
    def contributing_agents(self) -> list[str]:
        return ["self-heal", "escalation"]

    @property
    def priority(self) -> float:
        return 2.5  # Highest priority — must be healthy to pursue other goals

    @property
    def critical_threshold(self) -> float:
        return 0.4

    async def evaluate(
        self,
        state: OrchestratorState,
        context: GoalContext,
    ) -> GoalSnapshot:
        summary = context.current_summary or state.last_run

        if summary is None:
            return GoalSnapshot(goal_id=self.goal_id, score=1.0, details={"note": "First run"})

        # Error rate from current/last run
        error_count = len(summary.errors)
        error_score = max(0.0, 1.0 - (error_count * 0.2))

        # Self-heal issues indicate known problems
        self_heal_issues = summary.self_heal_local_issues
        heal_score = max(0.0, 1.0 - (self_heal_issues * 0.15))

        # Stuck-loop detection: same errors appearing across consecutive runs
        repeated_errors = 0
        if len(state.run_history) >= 2:
            recent_error_sets = [set(r.errors) for r in state.run_history[-3:]]
            if len(recent_error_sets) >= 2:
                common = recent_error_sets[0]
                for err_set in recent_error_sets[1:]:
                    common = common & err_set
                repeated_errors = len(common)

        loop_score = max(0.0, 1.0 - (repeated_errors * 0.3))

        score = round(
            max(
                0.0,
                min(
                    1.0,
                    error_score * 0.4 + heal_score * 0.3 + loop_score * 0.3,
                ),
            ),
            3,
        )

        return GoalSnapshot(
            goal_id=self.goal_id,
            score=score,
            details={
                "error_count": error_count,
                "self_heal_issues": self_heal_issues,
                "repeated_errors": repeated_errors,
                "error_score": round(error_score, 3),
                "heal_score": round(heal_score, 3),
                "loop_score": round(loop_score, 3),
            },
        )


# ── G7: Documentation Currency ──────────────────────────────────


class DocumentationCurrencyGoal(Goal):
    """Documentation should reflect recently merged changes."""

    @property
    def goal_id(self) -> str:
        return "documentation"

    @property
    def description(self) -> str:
        return "Documentation and changelog up-to-date with merged changes"

    @property
    def contributing_agents(self) -> list[str]:
        return ["docs"]

    @property
    def priority(self) -> float:
        return 0.5

    @property
    def satisfaction_threshold(self) -> float:
        return 0.8

    async def evaluate(
        self,
        state: OrchestratorState,
        context: GoalContext,
    ) -> GoalSnapshot:
        summary = context.current_summary or state.last_run

        if summary is None:
            return GoalSnapshot(goal_id=self.goal_id, score=1.0, details={"note": "No run data"})

        analyzed = summary.docs_prs_analyzed
        doc_pr = summary.docs_pr_opened

        if analyzed == 0:
            score = 1.0  # Nothing to document
        elif doc_pr is not None:
            score = 0.8  # PR opened but not merged yet
        else:
            score = 0.5  # PRs found but no doc update

        return GoalSnapshot(
            goal_id=self.goal_id,
            score=round(score, 3),
            details={"prs_analyzed": analyzed, "doc_pr_opened": doc_pr},
        )


# ── Factory ──────────────────────────────────────────────────────

ALL_GOALS: list[type[Goal]] = [
    CIHealthGoal,
    PRLifecycleGoal,
    IssueTriageGoal,
    SecurityPostureGoal,
    UpgradeCurrencyGoal,
    SelfHealthGoal,
    DocumentationCurrencyGoal,
]


def build_goals() -> list[Goal]:
    """Construct all default goal instances."""
    return [cls() for cls in ALL_GOALS]
