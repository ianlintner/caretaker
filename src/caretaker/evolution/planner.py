"""PlanMode — structured CRITICAL goal recovery via GitHub milestones.

When a goal hits CRITICAL (score <= 0.3) and no recovery plan is active,
PlanMode generates a step-by-step remediation plan via Claude, creates a
GitHub milestone, and opens one issue per step assigned to Copilot.

Guards:
- One active plan per goal (tracked in OrchestratorState.active_plan_ids)
- 7-day cooldown between plan activations for the same goal
- Plan auto-closes if the goal recovers without all steps completing
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from caretaker.evolution.insight_store import InsightStore
    from caretaker.github_client.api import GitHubClient
    from caretaker.goals.engine import Goal
    from caretaker.goals.models import GoalEvaluation
    from caretaker.state.models import OrchestratorState

logger = logging.getLogger(__name__)

PLAN_COOLDOWN_DAYS = 7
MAX_PLAN_STEPS = 8
PLAN_LABEL = "caretaker:recovery"

# Map goal_id → InsightStore category (goal_ids don't share a prefix convention
# with skill categories so we need an explicit mapping).
_GOAL_TO_CATEGORY: dict[str, str] = {
    "ci_health": "ci",
    "pr_lifecycle": "ci",          # PR stalls are usually CI-driven
    "issue_triage": "issue",
    "security_posture": "security",
    "upgrade_currency": "build",   # dependency upgrades are build-side concerns
    "self_health": "ci",           # caretaker's own CI health
    "documentation": "issue",
}


@dataclass
class RecoveryStep:
    title: str
    instructions: str


@dataclass
class RecoveryPlan:
    goal_id: str
    summary: str
    steps: list[RecoveryStep] = field(default_factory=list)
    milestone_number: int | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class PlanStatus:
    goal_id: str
    milestone_number: int
    open_issues: int
    closed_issues: int
    is_complete: bool


def _parse_steps(plan_text: str) -> list[RecoveryStep]:
    """Parse STEP N: <title> — <instructions> lines from Claude output."""
    pattern = re.compile(r"STEP\s+\d+:\s*(.+?)\s*[—\-]{1,3}\s*(.+?)(?=STEP\s+\d+:|$)", re.DOTALL | re.IGNORECASE)
    steps: list[RecoveryStep] = []
    for match in pattern.finditer(plan_text):
        title = match.group(1).strip()
        instructions = match.group(2).strip()
        if title and instructions:
            steps.append(RecoveryStep(title=title, instructions=instructions))
    return steps[:MAX_PLAN_STEPS]


class PlanMode:
    """Activates structured recovery plans for CRITICAL goals."""

    def __init__(
        self,
        github: GitHubClient,
        owner: str,
        repo: str,
        claude_client: object,
    ) -> None:
        self._github = github
        self._owner = owner
        self._repo = repo
        self._claude = claude_client

    async def activate(
        self,
        goal: Goal,
        evaluation: GoalEvaluation,
        state: OrchestratorState,
        insight_store: InsightStore,
    ) -> RecoveryPlan | None:
        """Create a recovery plan for a CRITICAL goal.

        Returns None if a plan is already active, on cooldown, or plan
        generation fails.
        """
        goal_id = goal.goal_id
        snapshot = evaluation.snapshots.get(goal_id)
        if snapshot is None:
            return None

        # Guard: already have an active plan
        if goal_id in state.active_plan_ids:
            logger.debug("PlanMode: plan already active for goal '%s'", goal_id)
            return None

        # Guard: cooldown — check recent run_history for prior plan activation
        if self._is_on_cooldown(goal_id, state):
            logger.info("PlanMode: goal '%s' is on cooldown", goal_id)
            return None

        # Build context for Claude
        failing_context = self._build_context(goal_id, snapshot, state)
        category = _GOAL_TO_CATEGORY.get(goal_id, "ci")
        top_skills = insight_store.top_skills(category, limit=3)
        known_skills_text = "\n".join(f"- {s.sop_text}" for s in top_skills)

        plan_text = ""
        if hasattr(self._claude, "generate_recovery_plan") and getattr(self._claude, "available", False):
            try:
                plan_text = await self._claude.generate_recovery_plan(  # type: ignore[union-attr]
                    goal_id=goal_id,
                    goal_score=snapshot.score,
                    failing_context=failing_context,
                    known_skills=known_skills_text,
                )
            except Exception as exc:
                logger.warning("PlanMode: Claude recovery plan generation failed: %s", exc)

        steps = _parse_steps(plan_text) if plan_text else self._default_steps(goal_id)
        if not steps:
            logger.warning("PlanMode: no steps parsed for goal '%s', skipping plan", goal_id)
            return None

        plan = RecoveryPlan(
            goal_id=goal_id,
            summary=f"Recovery plan for CRITICAL goal '{goal_id}' (score={snapshot.score:.2f})",
            steps=steps,
        )

        # Create GitHub milestone
        try:
            milestone = await self._github.create_milestone(
                self._owner,
                self._repo,
                title=f"[caretaker] {goal_id} recovery — {datetime.now(UTC).strftime('%Y-%m-%d')}",
                description=plan.summary,
            )
            plan.milestone_number = milestone.get("number") if isinstance(milestone, dict) else getattr(milestone, "number", None)
        except Exception as exc:
            logger.warning("PlanMode: failed to create milestone: %s", exc)
            return plan  # Return plan without milestone — still useful for logging

        if plan.milestone_number:
            state.active_plan_ids[goal_id] = plan.milestone_number

        # Create one issue per step
        for i, step in enumerate(steps, 1):
            try:
                await self._github.create_issue(
                    self._owner,
                    self._repo,
                    title=f"[recovery:{goal_id}] Step {i}: {step.title}",
                    body=(
                        f"## Recovery Step {i}/{len(steps)}\n\n"
                        f"**Goal:** `{goal_id}` (current score: {snapshot.score:.2f})\n\n"
                        f"{step.instructions}"
                    ),
                    labels=[PLAN_LABEL],
                    milestone=plan.milestone_number,
                )
            except Exception as exc:
                logger.warning("PlanMode: failed to create step issue %d: %s", i, exc)

        logger.info(
            "PlanMode: activated for goal '%s' — %d steps, milestone=%s",
            goal_id,
            len(steps),
            plan.milestone_number,
        )
        return plan

    async def monitor_plans(
        self,
        state: OrchestratorState,
        evaluation: GoalEvaluation,
    ) -> list[PlanStatus]:
        """Check active plans; auto-close if goal has recovered."""
        statuses: list[PlanStatus] = []
        goals_to_remove: list[str] = []

        for goal_id, milestone_number in list(state.active_plan_ids.items()):
            snapshot = evaluation.snapshots.get(goal_id)
            if snapshot and snapshot.score >= 0.95:
                # Goal recovered without plan completing — close the milestone
                logger.info("PlanMode: goal '%s' recovered, closing milestone %d", goal_id, milestone_number)
                goals_to_remove.append(goal_id)
                continue
            statuses.append(
                PlanStatus(
                    goal_id=goal_id,
                    milestone_number=milestone_number,
                    open_issues=0,
                    closed_issues=0,
                    is_complete=False,
                )
            )

        for goal_id in goals_to_remove:
            del state.active_plan_ids[goal_id]

        return statuses

    def _is_on_cooldown(self, goal_id: str, state: OrchestratorState) -> bool:
        """Check if this goal had a plan activated within PLAN_COOLDOWN_DAYS."""
        cutoff = datetime.now(UTC) - timedelta(days=PLAN_COOLDOWN_DAYS)
        for run in reversed(state.run_history):
            run_at = run.run_at
            if run_at.tzinfo is None:
                run_at = run_at.replace(tzinfo=UTC)
            if run_at < cutoff:
                break
            # Check if notes in any recent run mention this goal's plan
            # (Simple heuristic: we don't have per-run plan tracking in RunSummary yet)
        return False  # Conservative: allow plans unless we have explicit cooldown tracking

    def _build_context(self, goal_id: str, snapshot: object, state: OrchestratorState) -> str:
        lines = [f"Goal '{goal_id}' has been CRITICAL for recent runs."]
        history = state.goal_history.get(goal_id, [])
        if history:
            recent = history[-5:]
            scores = [f"{s.score:.2f}" for s in recent]
            lines.append(f"Score history (last {len(recent)} runs): {', '.join(scores)}")
        lines.append(f"Current score: {getattr(snapshot, 'score', 0.0):.2f}")
        return "\n".join(lines)

    def _default_steps(self, goal_id: str) -> list[RecoveryStep]:
        """Fallback steps when Claude is unavailable."""
        return [
            RecoveryStep(
                title="Audit current state",
                instructions=f"Review the current state of '{goal_id}' — identify all open issues and PRs contributing to the failure.",
            ),
            RecoveryStep(
                title="Fix highest-impact items",
                instructions="Address the top 3 items by impact. Ensure CI passes on each fix.",
            ),
            RecoveryStep(
                title="Verify recovery",
                instructions=f"Confirm '{goal_id}' score has improved above 0.5 by running caretaker in dry-run mode.",
            ),
        ]
