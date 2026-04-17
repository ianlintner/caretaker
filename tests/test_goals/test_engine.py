"""Tests for goal engine and divergence detection."""

from __future__ import annotations

from unittest.mock import AsyncMock

from caretaker.config import GoalEngineConfig, MaintainerConfig
from caretaker.goals.engine import DivergenceDetector, Goal, GoalContext, GoalEngine
from caretaker.goals.models import GoalEvaluation, GoalSnapshot, GoalStatus
from caretaker.state.models import OrchestratorState


class DummyGoal(Goal):
    def __init__(
        self,
        *,
        goal_id: str,
        score: float,
        agents: list[str],
        priority: float = 1.0,
        satisfaction_threshold: float = 0.95,
        critical_threshold: float = 0.3,
    ) -> None:
        self._goal_id = goal_id
        self._score = score
        self._agents = agents
        self._priority = priority
        self._satisfaction_threshold = satisfaction_threshold
        self._critical_threshold = critical_threshold

    @property
    def goal_id(self) -> str:
        return self._goal_id

    @property
    def description(self) -> str:
        return self._goal_id

    @property
    def contributing_agents(self) -> list[str]:
        return self._agents

    @property
    def priority(self) -> float:
        return self._priority

    @property
    def satisfaction_threshold(self) -> float:
        return self._satisfaction_threshold

    @property
    def critical_threshold(self) -> float:
        return self._critical_threshold

    async def evaluate(
        self,
        state: OrchestratorState,
        context: GoalContext,
    ) -> GoalSnapshot:
        return GoalSnapshot(goal_id=self.goal_id, score=self._score)


class ExplodingGoal(DummyGoal):
    async def evaluate(
        self,
        state: OrchestratorState,
        context: GoalContext,
    ) -> GoalSnapshot:
        raise RuntimeError("boom")


def make_context() -> GoalContext:
    return GoalContext(
        github=AsyncMock(),
        owner="o",
        repo="r",
        config=MaintainerConfig(),
    )


class TestDivergenceDetector:
    def test_analyze_returns_diverging_for_consecutive_declines(self) -> None:
        detector = DivergenceDetector(divergence_threshold=3, stale_threshold=5)
        goal = DummyGoal(goal_id="g", score=0.0, agents=["pr"])
        history = [
            GoalSnapshot(goal_id="g", score=0.9),
            GoalSnapshot(goal_id="g", score=0.8),
        ]
        current = GoalSnapshot(goal_id="g", score=0.7)

        status = detector.analyze(goal, history, current)

        assert status == GoalStatus.DIVERGING

    def test_analyze_returns_stale_for_flat_scores(self) -> None:
        detector = DivergenceDetector(divergence_threshold=3, stale_threshold=4)
        goal = DummyGoal(goal_id="g", score=0.0, agents=["pr"])
        history = [
            GoalSnapshot(goal_id="g", score=0.4),
            GoalSnapshot(goal_id="g", score=0.4),
            GoalSnapshot(goal_id="g", score=0.4),
        ]
        current = GoalSnapshot(goal_id="g", score=0.4)

        status = detector.analyze(goal, history, current)

        assert status == GoalStatus.STALE


class TestGoalEngine:
    async def test_evaluate_all_builds_dispatch_and_escalations(self) -> None:
        goals: list[Goal] = [
            DummyGoal(goal_id="critical", score=0.2, agents=["security"], priority=2.0),
            DummyGoal(goal_id="healthy", score=1.0, agents=["docs"], priority=1.0),
            DummyGoal(goal_id="weak", score=0.6, agents=["pr"], priority=1.5),
        ]
        engine = GoalEngine(goals, GoalEngineConfig())
        state = OrchestratorState()

        evaluation = await engine.evaluate_all(state, make_context())

        assert isinstance(evaluation, GoalEvaluation)
        assert evaluation.overall_health == 0.5111
        assert evaluation.dispatch_plan[0] == "security"
        assert "pr" in evaluation.dispatch_plan
        assert all(agent != "docs" for agent in evaluation.dispatch_plan)
        assert len(evaluation.escalations) == 1
        assert evaluation.escalations[0].goal_id == "critical"
        assert evaluation.escalations[0].status == GoalStatus.CRITICAL

    async def test_evaluate_all_handles_goal_exception(self) -> None:
        goals: list[Goal] = [ExplodingGoal(goal_id="boom", score=0.0, agents=["self-heal"])]
        engine = GoalEngine(goals, GoalEngineConfig())

        evaluation = await engine.evaluate_all(OrchestratorState(), make_context())

        assert evaluation.snapshots["boom"].status == GoalStatus.CRITICAL
        assert evaluation.snapshots["boom"].score == 0.0
        assert "error" in evaluation.snapshots["boom"].details
        assert evaluation.escalations[0].goal_id == "boom"

    def test_record_evaluation_trims_history(self) -> None:
        engine = GoalEngine(
            [DummyGoal(goal_id="g", score=0.7, agents=["pr"])],
            GoalEngineConfig(max_history=2),
        )
        state = OrchestratorState()

        engine.record_evaluation(
            state,
            GoalEvaluation(
                snapshots={"g": GoalSnapshot(goal_id="g", score=0.5)},
            ),
        )
        engine.record_evaluation(
            state,
            GoalEvaluation(
                snapshots={"g": GoalSnapshot(goal_id="g", score=0.6)},
            ),
        )
        engine.record_evaluation(
            state,
            GoalEvaluation(
                snapshots={"g": GoalSnapshot(goal_id="g", score=0.7)},
            ),
        )

        assert [s.score for s in state.goal_history["g"]] == [0.6, 0.7]
