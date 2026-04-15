"""Goal engine — evaluates goals, detects divergence, plans agent dispatch."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING

from caretaker.goals.models import (
    GoalEscalation,
    GoalEvaluation,
    GoalSnapshot,
    GoalStatus,
)

if TYPE_CHECKING:
    from caretaker.config import GoalEngineConfig, MaintainerConfig
    from caretaker.github_client.api import GitHubClient
    from caretaker.registry import AgentRegistry
    from caretaker.state.models import OrchestratorState, RunSummary

logger = logging.getLogger(__name__)


@dataclass
class GoalContext:
    """Read-only context available to goal evaluators during scoring."""

    github: GitHubClient
    owner: str
    repo: str
    config: MaintainerConfig
    current_summary: RunSummary | None = None


class Goal(ABC):
    """Abstract base for a quantitatively measurable maintenance goal.

    Each goal produces a score between 0.0 (completely unmet) and 1.0
    (fully satisfied).  The engine uses these scores to prioritise
    agent dispatch and detect divergence from healthy repository state.
    """

    @property
    @abstractmethod
    def goal_id(self) -> str:
        """Unique identifier for this goal."""

    @property
    @abstractmethod
    def description(self) -> str:
        """Human-readable description of what this goal measures."""

    @property
    @abstractmethod
    def contributing_agents(self) -> list[str]:
        """Agent names that work toward satisfying this goal."""

    @property
    def priority(self) -> float:
        """Weight for dispatch ordering.  Higher = more urgent."""
        return 1.0

    @property
    def satisfaction_threshold(self) -> float:
        """Score at or above which the goal is considered satisfied."""
        return 0.95

    @property
    def critical_threshold(self) -> float:
        """Score at or below which the goal is critical."""
        return 0.3

    @abstractmethod
    async def evaluate(
        self,
        state: OrchestratorState,
        context: GoalContext,
    ) -> GoalSnapshot:
        """Compute the current goal score from persisted and live state."""

    def validate_agents(self, registry: AgentRegistry) -> list[str]:
        """Return warnings if contributing agents are missing or disabled."""
        issues: list[str] = []
        for name in self.contributing_agents:
            agent = registry.get(name)
            if agent is None:
                issues.append(f"Goal '{self.goal_id}': agent '{name}' not registered")
            elif not agent.enabled():
                issues.append(
                    f"Goal '{self.goal_id}': agent '{name}' is disabled "
                    "— goal may not be achievable"
                )
        return issues


# ── Divergence Detection ──────────────────────────────────────────


class DivergenceDetector:
    """Analyses goal score history to detect stale or diverging trends."""

    def __init__(
        self,
        divergence_threshold: int = 3,
        stale_threshold: int = 5,
    ) -> None:
        self._divergence_threshold = divergence_threshold
        self._stale_threshold = stale_threshold

    def analyze(
        self,
        goal: Goal,
        history: list[GoalSnapshot],
        current: GoalSnapshot,
    ) -> GoalStatus:
        """Determine goal status from its score trajectory."""
        if current.score >= goal.satisfaction_threshold:
            return GoalStatus.SATISFIED

        if current.score <= goal.critical_threshold:
            return GoalStatus.CRITICAL

        all_scores = [s.score for s in history] + [current.score]

        if len(all_scores) < 2:
            return GoalStatus.PROGRESSING

        # Diverging: N consecutive score declines
        recent = all_scores[-self._divergence_threshold :]
        if len(recent) >= self._divergence_threshold and all(
            recent[i] > recent[i + 1] for i in range(len(recent) - 1)
        ):
            return GoalStatus.DIVERGING

        # Stale: M consecutive unchanged scores below satisfaction
        stale_window = all_scores[-self._stale_threshold :]
        if len(stale_window) >= self._stale_threshold:
            rounded = [round(s, 3) for s in stale_window]
            if len(set(rounded)) == 1 and rounded[0] < goal.satisfaction_threshold:
                return GoalStatus.STALE

        # Oscillation detection: alternating improve/decline for N runs
        if len(all_scores) >= self._divergence_threshold + 1:
            tail = all_scores[-(self._divergence_threshold + 1) :]
            deltas = [tail[i + 1] - tail[i] for i in range(len(tail) - 1)]
            sign_changes = sum(1 for i in range(len(deltas) - 1) if deltas[i] * deltas[i + 1] < 0)
            if sign_changes >= len(deltas) - 1 and len(deltas) >= 3:
                return GoalStatus.STALE

        return GoalStatus.PROGRESSING


# ── Goal Engine ───────────────────────────────────────────────────


class GoalEngine:
    """Central engine that evaluates goals and drives agent dispatch."""

    def __init__(
        self,
        goals: list[Goal],
        config: GoalEngineConfig,
    ) -> None:
        self._goals: dict[str, Goal] = {g.goal_id: g for g in goals}
        self._config = config
        self._divergence = DivergenceDetector(
            divergence_threshold=config.divergence_threshold,
            stale_threshold=config.stale_threshold,
        )

    @property
    def goals(self) -> dict[str, Goal]:
        return dict(self._goals)

    def validate(self, registry: AgentRegistry) -> list[str]:
        """Check that all goals' contributing agents are registered and enabled."""
        issues: list[str] = []
        for goal in self._goals.values():
            issues.extend(goal.validate_agents(registry))
        return issues

    async def evaluate_all(
        self,
        state: OrchestratorState,
        context: GoalContext,
    ) -> GoalEvaluation:
        """Evaluate every registered goal and produce a dispatch plan."""
        snapshots: dict[str, GoalSnapshot] = {}
        escalations: list[GoalEscalation] = []

        for goal in self._goals.values():
            try:
                snapshot = await goal.evaluate(state, context)
            except Exception as exc:
                logger.error("Goal '%s' evaluation failed: %s", goal.goal_id, exc)
                snapshot = GoalSnapshot(
                    goal_id=goal.goal_id,
                    score=0.0,
                    status=GoalStatus.CRITICAL,
                    details={"error": str(exc)},
                )

            history = state.goal_history.get(goal.goal_id, [])
            snapshot.status = self._divergence.analyze(goal, history, snapshot)
            snapshots[goal.goal_id] = snapshot

            if snapshot.status in (
                GoalStatus.CRITICAL,
                GoalStatus.DIVERGING,
                GoalStatus.STALE,
            ):
                escalations.append(self._build_escalation(goal, snapshot, history))

        total_weight = sum(g.priority for g in self._goals.values())
        overall = (
            sum(snapshots[gid].score * g.priority for gid, g in self._goals.items()) / total_weight
            if total_weight > 0
            else 0.0
        )

        dispatch_plan = self._build_dispatch_plan(snapshots)

        return GoalEvaluation(
            snapshots=snapshots,
            overall_health=round(overall, 4),
            dispatch_plan=dispatch_plan,
            escalations=escalations,
        )

    def record_evaluation(
        self,
        state: OrchestratorState,
        evaluation: GoalEvaluation,
    ) -> None:
        """Persist goal snapshots into orchestrator state history."""
        max_history = self._config.max_history
        for goal_id, snapshot in evaluation.snapshots.items():
            if goal_id not in state.goal_history:
                state.goal_history[goal_id] = []
            state.goal_history[goal_id].append(snapshot)
            if len(state.goal_history[goal_id]) > max_history:
                state.goal_history[goal_id] = state.goal_history[goal_id][-max_history:]

    # ── Private helpers ──────────────────────────────────────────

    def _build_dispatch_plan(
        self,
        snapshots: dict[str, GoalSnapshot],
    ) -> list[str]:
        """Order agents by how urgently their associated goals need attention."""
        agent_urgency: dict[str, float] = {}

        for gid, snapshot in snapshots.items():
            goal = self._goals[gid]
            if snapshot.score >= goal.satisfaction_threshold:
                continue

            urgency = (1.0 - snapshot.score) * goal.priority

            if snapshot.status in (GoalStatus.CRITICAL, GoalStatus.DIVERGING):
                urgency *= 2.0
            elif snapshot.status == GoalStatus.STALE:
                urgency *= 1.5

            for agent_name in goal.contributing_agents:
                agent_urgency[agent_name] = max(
                    agent_urgency.get(agent_name, 0.0),
                    urgency,
                )

        return sorted(
            agent_urgency.keys(),
            key=lambda a: agent_urgency[a],
            reverse=True,
        )

    def _build_escalation(
        self,
        goal: Goal,
        snapshot: GoalSnapshot,
        history: list[GoalSnapshot],
    ) -> GoalEscalation:
        """Build an escalation record for a troubled goal."""
        consecutive = 0
        for past in reversed(history):
            if past.status in (
                GoalStatus.CRITICAL,
                GoalStatus.DIVERGING,
                GoalStatus.STALE,
            ):
                consecutive += 1
            else:
                break

        reason_map = {
            GoalStatus.CRITICAL: (
                f"Score {snapshot.score:.2f} below critical threshold {goal.critical_threshold}"
            ),
            GoalStatus.DIVERGING: (f"Score declining for {consecutive + 1} consecutive runs"),
            GoalStatus.STALE: (
                f"Score unchanged at {snapshot.score:.2f} for {consecutive + 1} consecutive runs"
            ),
        }

        action_map = {
            GoalStatus.CRITICAL: "Immediate human review required",
            GoalStatus.DIVERGING: "Investigate root cause of regression",
            GoalStatus.STALE: "Review agent effectiveness and unblock progress",
        }

        return GoalEscalation(
            goal_id=goal.goal_id,
            status=snapshot.status,
            score=snapshot.score,
            reason=reason_map.get(snapshot.status, f"Goal unhealthy: {snapshot.status}"),
            consecutive_runs=consecutive + 1,
            recommended_action=action_map.get(snapshot.status, "Review goal configuration"),
        )
