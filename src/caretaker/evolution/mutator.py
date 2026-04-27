"""StrategyMutator — hill-climbing config parameter mutation engine.

Trials one config parameter change at a time, measures its effect over 3+
orchestrator runs, then accepts (opens a PR to update config.yaml) or rejects
(reverts, tries next mutation) based on goal score delta.

Constraints:
- At most MAX_CONCURRENT active mutations across all agents
- Minimum MIN_RUNS_BEFORE_EVAL runs before evaluating a mutation
- One mutation per agent parameter at a time
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from caretaker.config import MaintainerConfig
    from caretaker.evolution.insight_store import InsightStore, Mutation
    from caretaker.evolution.reflection import ReflectionResult, StrategyRecommendation
    from caretaker.goals.models import GoalEvaluation
    from caretaker.state.models import OrchestratorState

logger = logging.getLogger(__name__)

MAX_CONCURRENT = 2
MIN_RUNS_BEFORE_EVAL = 3
ACCEPTANCE_DELTA = 0.05  # goal score must improve by at least this to accept

# Parameters available for mutation and their (min, max) bounds
MUTABLE_PARAMETERS: dict[str, dict[str, tuple[int | float, int | float]]] = {
    "pr_agent": {
        "copilot_max_retries": (1, 5),
        "retry_window_hours": (1, 24),
    },
    "devops_agent": {
        "cooldown_hours": (2, 48),
        "max_issues_per_run": (1, 10),
    },
    "issue_agent": {
        "auto_close_stale_days": (7, 90),
    },
    "goal_engine": {
        "divergence_threshold": (2, 7),
        "stale_threshold": (3, 10),
    },
}


@dataclass
class MutationOutcome:
    mutation_id: str
    agent_name: str
    parameter: str
    old_value: str
    new_value: str
    outcome: str  # "accepted" | "rejected"
    score_delta: float


def _get_config_value(config: MaintainerConfig, agent_name: str, parameter: str) -> Any:
    """Read a current config value by agent_name.parameter path."""
    agent_cfg = getattr(config, agent_name, None)
    if agent_cfg is None:
        return None
    # Handle nested copilot sub-config for pr_agent
    if agent_name == "pr_agent" and parameter in ("copilot_max_retries", "retry_window_hours"):
        copilot_cfg = getattr(agent_cfg, "copilot", None)
        if copilot_cfg is None:
            return None
        param = "max_retries" if parameter == "copilot_max_retries" else parameter
        return getattr(copilot_cfg, param, None)
    return getattr(agent_cfg, parameter, None)


def _apply_mutation_to_config(
    config: MaintainerConfig, agent_name: str, parameter: str, new_value: str
) -> MaintainerConfig:
    """Return a shallow copy of config with one parameter overridden.

    Uses model_copy() for Pydantic v2 nested updates.
    """
    try:
        raw: int | float = float(new_value) if "." in new_value else int(new_value)
    except ValueError:
        raw = new_value  # type: ignore[assignment]

    if agent_name == "pr_agent" and parameter in ("copilot_max_retries", "retry_window_hours"):
        old_copilot = config.pr_agent.copilot
        param = "max_retries" if parameter == "copilot_max_retries" else parameter
        new_copilot = old_copilot.model_copy(update={param: raw})
        new_pr = config.pr_agent.model_copy(update={"copilot": new_copilot})
        return config.model_copy(update={"pr_agent": new_pr})

    old_agent = getattr(config, agent_name)
    new_agent = old_agent.model_copy(update={parameter: raw})
    return config.model_copy(update={agent_name: new_agent})


class StrategyMutator:
    """Proposes, applies, and evaluates single-parameter config mutations."""

    def __init__(self, insight_store: InsightStore) -> None:
        self._store = insight_store

    def propose_mutation(
        self,
        reflection: ReflectionResult,
        state: OrchestratorState,
        config: MaintainerConfig,
    ) -> Mutation | None:
        """Propose a mutation based on reflection recommendations.

        Returns None if the system is at MAX_CONCURRENT active mutations,
        the recommendation targets an unknown parameter, or the recommended
        value is out of bounds.
        """
        from caretaker.evolution.insight_store import Mutation as MutationModel

        active = self._store.active_mutations()
        if len(active) >= MAX_CONCURRENT:
            logger.info(
                "Mutation proposal skipped: %d active mutations (max %d)",
                len(active),
                MAX_CONCURRENT,
            )
            return None

        active_keys = {(m.agent_name, m.parameter) for m in active}

        for rec in reflection.recommendations:
            if not self._is_valid_recommendation(rec):
                continue
            if (rec.agent_name, rec.parameter) in active_keys:
                continue

            current = _get_config_value(config, rec.agent_name, rec.parameter)
            if current is None:
                continue

            mutation = MutationModel(
                id=str(uuid.uuid4()),
                agent_name=rec.agent_name,
                parameter=rec.parameter,
                old_value=str(current),
                new_value=rec.suggested_value,
                goal_id=reflection.triggered_by[0] if reflection.triggered_by else "unknown",
                goal_score_before=self._score_for_goal(
                    state, reflection.triggered_by[0] if reflection.triggered_by else ""
                ),
                goal_score_after=None,
                runs_evaluated=0,
                started_at=datetime.now(UTC),
                ended_at=None,
                outcome="pending",
            )
            self._store.upsert_mutation(mutation)
            logger.info(
                "Mutation proposed: %s.%s %s → %s (goal=%s)",
                rec.agent_name,
                rec.parameter,
                current,
                rec.suggested_value,
                mutation.goal_id,
            )
            return mutation

        return None

    def apply_pending(self, config: MaintainerConfig, state: OrchestratorState) -> MaintainerConfig:
        """Return a patched MaintainerConfig with all active mutations applied.

        Does NOT write config.yaml — mutations are in-memory for this run only
        until accepted.
        """
        active = self._store.active_mutations()
        patched = config
        for mutation in active:
            try:
                patched = _apply_mutation_to_config(
                    patched, mutation.agent_name, mutation.parameter, mutation.new_value
                )
                logger.debug(
                    "Applied mutation %s: %s.%s = %s",
                    mutation.id[:8],
                    mutation.agent_name,
                    mutation.parameter,
                    mutation.new_value,
                )
            except Exception as exc:
                logger.warning("Failed to apply mutation %s: %s", mutation.id[:8], exc)
        return patched

    def evaluate_pending(
        self,
        state: OrchestratorState,
        post_eval: GoalEvaluation,
    ) -> list[MutationOutcome]:
        """Increment run counters and accept/reject mutations that are ready."""
        active = self._store.active_mutations()
        outcomes: list[MutationOutcome] = []

        for mutation in active:
            mutation.runs_evaluated += 1

            current_score = self._score_for_goal(state, mutation.goal_id)
            if current_score is not None:
                mutation.goal_score_after = current_score

            if mutation.runs_evaluated < MIN_RUNS_BEFORE_EVAL:
                self._store.upsert_mutation(mutation)
                continue

            before = mutation.goal_score_before or 0.0
            after = mutation.goal_score_after or 0.0
            delta = after - before

            if delta >= ACCEPTANCE_DELTA:
                mutation.outcome = "accepted"
                mutation.ended_at = datetime.now(UTC)
                self._store.upsert_mutation(mutation)
                outcomes.append(
                    MutationOutcome(
                        mutation_id=mutation.id,
                        agent_name=mutation.agent_name,
                        parameter=mutation.parameter,
                        old_value=mutation.old_value,
                        new_value=mutation.new_value,
                        outcome="accepted",
                        score_delta=delta,
                    )
                )
                logger.info(
                    "Mutation accepted: %s.%s %s → %s (Δ=+%.3f)",
                    mutation.agent_name,
                    mutation.parameter,
                    mutation.old_value,
                    mutation.new_value,
                    delta,
                )
            else:
                mutation.outcome = "rejected"
                mutation.ended_at = datetime.now(UTC)
                self._store.upsert_mutation(mutation)
                outcomes.append(
                    MutationOutcome(
                        mutation_id=mutation.id,
                        agent_name=mutation.agent_name,
                        parameter=mutation.parameter,
                        old_value=mutation.old_value,
                        new_value=mutation.new_value,
                        outcome="rejected",
                        score_delta=delta,
                    )
                )
                logger.info(
                    "Mutation rejected: %s.%s %s → %s (Δ=%.3f, needed ≥%.3f)",
                    mutation.agent_name,
                    mutation.parameter,
                    mutation.old_value,
                    mutation.new_value,
                    delta,
                    ACCEPTANCE_DELTA,
                )

        return outcomes

    def _is_valid_recommendation(self, rec: StrategyRecommendation) -> bool:
        params = MUTABLE_PARAMETERS.get(rec.agent_name, {})
        if rec.parameter not in params:
            return False
        lo, hi = params[rec.parameter]
        try:
            val = float(rec.suggested_value)
            return lo <= val <= hi
        except (ValueError, TypeError):
            return False

    def _score_for_goal(self, state: OrchestratorState, goal_id: str) -> float | None:
        history = state.goal_history.get(goal_id, [])
        if not history:
            return None
        return history[-1].score
