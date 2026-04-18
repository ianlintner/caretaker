"""ReflectionEngine — post-run Claude analysis when goals are stuck.

Implements the Reflexion pattern: when a goal is STALE for N consecutive runs
or DIVERGING for M runs, the engine generates a Claude analysis that identifies
root causes and recommends strategy changes.  Output is posted as a labeled
comment on the tracking issue and optionally parsed into StrategyRecommendations
for the StrategyMutator to act on.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from caretaker.goals.models import GoalStatus

if TYPE_CHECKING:
    from caretaker.evolution.insight_store import InsightStore
    from caretaker.goals.models import GoalEvaluation
    from caretaker.state.models import OrchestratorState, RunSummary

logger = logging.getLogger(__name__)

REFLECTION_COMMENT_MARKER = "<!-- caretaker:reflection -->"
REFLECTION_COMMENT_CLOSE = "<!-- /caretaker:reflection -->"


@dataclass
class StrategyRecommendation:
    agent_name: str
    parameter: str
    suggested_value: str
    rationale: str


@dataclass
class ReflectionResult:
    analysis: str
    recommendations: list[StrategyRecommendation] = field(default_factory=list)
    reflected_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    triggered_by: list[str] = field(default_factory=list)  # goal_ids that triggered


def _count_consecutive_bad(goal_id: str, state: OrchestratorState) -> int:
    history = state.goal_history.get(goal_id, [])
    bad_statuses = {GoalStatus.STALE, GoalStatus.DIVERGING, GoalStatus.CRITICAL}
    count = 0
    for snap in reversed(history):
        if snap.status in bad_statuses:
            count += 1
        else:
            break
    return count


class ReflectionEngine:
    """Determines when to reflect and orchestrates the Claude analysis call."""

    DIVERGING_THRESHOLD = 3  # consecutive DIVERGING runs before reflection
    STALE_THRESHOLD = 5  # consecutive STALE runs before reflection
    HEALTH_THRESHOLD = 0.6  # self_health below this for 2+ runs triggers reflection
    HEALTH_DECLINE_RUNS = 5  # consecutive overall health declines

    def __init__(self) -> None:
        pass

    def should_reflect(
        self,
        evaluation: GoalEvaluation,
        state: OrchestratorState,
    ) -> bool:
        """Return True when at least one reflection trigger condition is met."""
        for goal_id, snapshot in evaluation.snapshots.items():
            consecutive = _count_consecutive_bad(goal_id, state)
            if snapshot.status == GoalStatus.DIVERGING and consecutive >= self.DIVERGING_THRESHOLD:
                logger.debug("Reflection triggered: %s DIVERGING for %d runs", goal_id, consecutive)
                return True
            if snapshot.status == GoalStatus.STALE and consecutive >= self.STALE_THRESHOLD:
                logger.debug("Reflection triggered: %s STALE for %d runs", goal_id, consecutive)
                return True
            if goal_id == "self_health" and snapshot.score < self.HEALTH_THRESHOLD:
                recent_low = sum(
                    1
                    for s in list(state.goal_history.get(goal_id, []))[-2:]
                    if s.score < self.HEALTH_THRESHOLD
                )
                if recent_low >= 2:
                    logger.debug(
                        "Reflection triggered: self_health below %.1f", self.HEALTH_THRESHOLD
                    )
                    return True

        # Overall health declining across last N runs
        health_scores = [
            r.goal_health
            for r in state.run_history[-self.HEALTH_DECLINE_RUNS :]
            if r.goal_health is not None
        ]
        if len(health_scores) >= self.HEALTH_DECLINE_RUNS:
            if all(health_scores[i] > health_scores[i + 1] for i in range(len(health_scores) - 1)):
                logger.debug(
                    "Reflection triggered: overall health declining for %d runs",
                    self.HEALTH_DECLINE_RUNS,
                )
                return True

        return False

    def triggered_goals(
        self,
        evaluation: GoalEvaluation,
        state: OrchestratorState,
    ) -> list[str]:
        """Return goal_ids that contributed to the reflection trigger."""
        triggered: list[str] = []
        for goal_id, snapshot in evaluation.snapshots.items():
            consecutive = _count_consecutive_bad(goal_id, state)
            if (
                snapshot.status == GoalStatus.DIVERGING
                and consecutive >= self.DIVERGING_THRESHOLD
                or snapshot.status == GoalStatus.STALE
                and consecutive >= self.STALE_THRESHOLD
            ):
                triggered.append(goal_id)
        return triggered

    async def reflect(
        self,
        evaluation: GoalEvaluation,
        state: OrchestratorState,
        run_history: list[RunSummary],
        insight_store: InsightStore,
        claude_client: object,  # ClaudeClient — avoid circular import
    ) -> ReflectionResult:
        """Generate a reflection via Claude and return the parsed result."""
        triggered = self.triggered_goals(evaluation, state)
        prompt = self._build_prompt(evaluation, state, run_history, insight_store, triggered)

        analysis_text = ""
        try:
            # ClaudeClient.generate_reflection is injected at call time
            if hasattr(claude_client, "generate_reflection"):
                analysis_text = await claude_client.generate_reflection(prompt)  # type: ignore[union-attr]
        except Exception as exc:
            logger.warning("Reflection Claude call failed: %s", exc)
            analysis_text = f"[Reflection unavailable: {exc}]"

        recommendations = self._parse_recommendations(analysis_text)
        result = ReflectionResult(
            analysis=analysis_text,
            recommendations=recommendations,
            triggered_by=triggered,
        )
        logger.info(
            "Reflection generated: %d chars, %d recommendations, triggered_by=%s",
            len(analysis_text),
            len(recommendations),
            triggered,
        )
        return result

    def _build_prompt(
        self,
        evaluation: GoalEvaluation,
        state: OrchestratorState,
        run_history: list[RunSummary],
        insight_store: InsightStore,
        triggered: list[str],
    ) -> str:
        lines = [
            "You are analyzing why an autonomous repository maintenance system is failing to improve.",
            "",
            "## Current Goal States",
        ]
        for goal_id, snap in evaluation.snapshots.items():
            consecutive = _count_consecutive_bad(goal_id, state)
            marker = " ← TRIGGERED" if goal_id in triggered else ""
            lines.append(
                f"- {goal_id}: score={snap.score:.2f}, status={snap.status.value}, "
                f"consecutive_bad={consecutive}{marker}"
            )

        lines.extend(["", "## Last 10 Run Summaries (newest first)"])
        for summary in reversed(run_history[-10:]):
            health_str = f"{summary.goal_health:.2f}" if summary.goal_health is not None else "n/a"
            lines.append(
                f"- {summary.run_at.strftime('%Y-%m-%d %H:%M')}: "
                f"prs_merged={summary.prs_merged}, prs_escalated={summary.prs_escalated}, "
                f"goal_health={health_str}, errors={len(summary.errors)}"
            )

        lines.extend(["", "## Known Effective Skills"])
        from caretaker.evolution.insight_store import ALL_CATEGORIES

        for cat in sorted(ALL_CATEGORIES):
            top = insight_store.top_skills(cat, limit=3)
            if top:
                lines.append(f"**{cat}:**")
                for s in top:
                    lines.append(f"  - [{s.confidence:.0%}] {s.sop_text[:80]}")

        lines.extend(
            [
                "",
                "## Analysis Request",
                "Analyze the following and respond in under 400 words:",
                "1. Root cause of each stuck/diverging goal",
                "2. Strategy changes most likely to improve each",
                "3. Config parameters that should change (with specific values)",
                "   Format any config recommendation as: RECOMMEND: <agent>.<param> = <value>",
            ]
        )
        return "\n".join(lines)

    def _parse_recommendations(self, analysis: str) -> list[StrategyRecommendation]:
        """Parse RECOMMEND: lines from the Claude response."""
        import re

        recommendations: list[StrategyRecommendation] = []
        pattern = re.compile(r"RECOMMEND:\s*(\w+)\.(\w+)\s*=\s*(.+?)(?:\n|$)", re.IGNORECASE)
        for match in pattern.finditer(analysis):
            agent_name, parameter, value = match.group(1), match.group(2), match.group(3).strip()
            recommendations.append(
                StrategyRecommendation(
                    agent_name=agent_name,
                    parameter=parameter,
                    suggested_value=value,
                    rationale="parsed from reflection analysis",
                )
            )
        return recommendations


def format_reflection_comment(result: ReflectionResult) -> str:
    """Format a ReflectionResult as a GitHub comment body."""
    lines = [
        f"{REFLECTION_COMMENT_MARKER}",
        f"## Caretaker Reflection — {result.reflected_at.strftime('%Y-%m-%d %H:%M UTC')}",
        "",
        f"**Triggered by:** {', '.join(result.triggered_by) if result.triggered_by else 'overall health decline'}",
        "",
        result.analysis,
    ]
    if result.recommendations:
        lines.extend(["", "**Proposed strategy changes:**"])
        for rec in result.recommendations:
            lines.append(
                f"- `{rec.agent_name}.{rec.parameter}` → `{rec.suggested_value}`: {rec.rationale}"
            )
    lines.append(REFLECTION_COMMENT_CLOSE)
    return "\n".join(lines)
