"""Tests for the evolution layer — InsightStore, skill injection, reflection,
mutator, stuck detection.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from caretaker.evolution.crystallizer import SkillCrystallizer, _infer_category
from caretaker.evolution.insight_store import (
    CATEGORY_BUILD,
    CATEGORY_CI,
    InsightStore,
    Mutation,
    _skill_id,
)
from caretaker.evolution.mutator import StrategyMutator
from caretaker.evolution.reflection import ReflectionEngine
from caretaker.goals.models import GoalSnapshot, GoalStatus
from caretaker.llm.copilot import CopilotTask, TaskType
from caretaker.state.models import OrchestratorState, PRTrackingState, TrackedPR

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def store() -> InsightStore:
    return InsightStore(db_path=":memory:")


@pytest.fixture
def base_state() -> OrchestratorState:
    return OrchestratorState()


# ── InsightStore ──────────────────────────────────────────────────────────────


class TestInsightStore:
    def test_record_success_creates_skill(self, store: InsightStore) -> None:
        store.record_success(CATEGORY_CI, "jest_timeout", "Increase testTimeout in jest.config.js")
        skills = store.all_skills(CATEGORY_CI)
        assert len(skills) == 1
        assert skills[0].success_count == 1
        assert skills[0].fail_count == 0

    def test_record_failure_increments_fail(self, store: InsightStore) -> None:
        store.record_success(CATEGORY_CI, "jest_timeout", "Increase testTimeout")
        store.record_failure(CATEGORY_CI, "jest_timeout")
        skills = store.all_skills(CATEGORY_CI)
        assert skills[0].fail_count == 1

    def test_confidence_zero_below_threshold(self, store: InsightStore) -> None:
        store.record_success(CATEGORY_CI, "sig", "sop")
        skill = store.get_by_signature(CATEGORY_CI, "sig")
        assert skill is not None
        assert skill.confidence == 0.0  # total < 3

    def test_confidence_computed_above_threshold(self, store: InsightStore) -> None:
        for _ in range(4):
            store.record_success(CATEGORY_CI, "sig", "sop")
        skill = store.get_by_signature(CATEGORY_CI, "sig")
        assert skill is not None
        assert skill.confidence == 1.0

    def test_confidence_mixed_counts(self, store: InsightStore) -> None:
        for _ in range(3):
            store.record_success(CATEGORY_CI, "sig", "sop")
        store.record_failure(CATEGORY_CI, "sig")
        skill = store.get_by_signature(CATEGORY_CI, "sig")
        assert skill is not None
        assert skill.confidence == pytest.approx(0.75)

    def test_get_relevant_filters_low_confidence(self, store: InsightStore) -> None:
        store.record_success(CATEGORY_CI, "sig_a", "sop_a")  # total=1, conf=0
        for _ in range(4):
            store.record_success(CATEGORY_CI, "sig_b", "sop_b")  # total=4, conf=1.0

        relevant = store.get_relevant(CATEGORY_CI, "anything", min_confidence=0.5)
        assert len(relevant) == 1
        assert relevant[0].signature == "sig_b"

    def test_top_skills_ordering(self, store: InsightStore) -> None:
        for _ in range(3):
            store.record_success(CATEGORY_CI, "low", "low_sop")
        store.record_failure(CATEGORY_CI, "low")  # conf=0.75
        for _ in range(4):
            store.record_success(CATEGORY_CI, "high", "high_sop")  # conf=1.0

        top = store.top_skills(CATEGORY_CI, limit=2)
        assert top[0].signature == "high"  # higher confidence first

    def test_id_is_stable(self) -> None:
        id1 = _skill_id(CATEGORY_CI, "jest_timeout")
        id2 = _skill_id(CATEGORY_CI, "jest_timeout")
        assert id1 == id2

    def test_prune_zero_confidence(self, store: InsightStore) -> None:
        for _ in range(6):
            store.record_failure(CATEGORY_CI, "always_fails")
        removed = store.prune_low_confidence(min_attempts=5)
        assert removed == 1
        assert store.get_by_signature(CATEGORY_CI, "always_fails") is None

    def test_categories_isolated(self, store: InsightStore) -> None:
        for _ in range(4):
            store.record_success(CATEGORY_CI, "sig", "sop_ci")
        for _ in range(4):
            store.record_success(CATEGORY_BUILD, "sig", "sop_build")

        ci_skills = store.top_skills(CATEGORY_CI)
        build_skills = store.top_skills(CATEGORY_BUILD)
        assert len(ci_skills) == 1
        assert len(build_skills) == 1
        assert ci_skills[0].sop_text == "sop_ci"
        assert build_skills[0].sop_text == "sop_build"


# ── Mutation table ────────────────────────────────────────────────────────────


class TestMutationTable:
    def test_upsert_and_retrieve(self, store: InsightStore) -> None:
        m = Mutation(
            id="test-1",
            agent_name="pr_agent",
            parameter="copilot_max_retries",
            old_value="2",
            new_value="3",
            goal_id="ci_health",
            goal_score_before=0.4,
            goal_score_after=None,
            runs_evaluated=0,
            started_at=datetime.now(UTC),
            ended_at=None,
            outcome="pending",
        )
        store.upsert_mutation(m)
        active = store.active_mutations()
        assert len(active) == 1
        assert active[0].id == "test-1"

    def test_accepted_mutation_not_in_active(self, store: InsightStore) -> None:
        m = Mutation(
            id="test-2",
            agent_name="pr_agent",
            parameter="copilot_max_retries",
            old_value="2",
            new_value="3",
            goal_id="ci_health",
            goal_score_before=0.4,
            goal_score_after=0.6,
            runs_evaluated=3,
            started_at=datetime.now(UTC),
            ended_at=datetime.now(UTC),
            outcome="accepted",
        )
        store.upsert_mutation(m)
        assert store.active_mutations() == []


# ── Skill injection into CopilotTask ─────────────────────────────────────────


class TestSkillInjection:
    def _make_task(self) -> CopilotTask:
        return CopilotTask(
            task_type=TaskType.CI_FAILURE,
            job_name="test",
            error_output="jest timeout",
            instructions="fix it",
            attempt=1,
            max_attempts=2,
        )

    def test_no_skills_no_hints_block(self) -> None:
        task = self._make_task()
        task.enrich_with_skills([])
        comment = task.to_comment()
        assert "SKILL HINTS" not in comment

    def test_skills_appear_in_comment(self, store: InsightStore) -> None:
        for _ in range(4):
            store.record_success(CATEGORY_CI, "jest_timeout", "Increase testTimeout to 30000")

        task = self._make_task()
        skills = store.get_relevant(CATEGORY_CI, "jest_timeout")
        task.enrich_with_skills(skills)

        comment = task.to_comment()
        assert "SKILL HINTS" in comment
        assert "Increase testTimeout" in comment

    def test_max_three_skills_injected(self, store: InsightStore) -> None:
        for i in range(5):
            for _ in range(4):
                store.record_success(CATEGORY_CI, f"sig_{i}", f"sop_{i}")

        skills = store.top_skills(CATEGORY_CI, limit=5)
        task = self._make_task()
        task.enrich_with_skills(skills)

        comment = task.to_comment()
        # Should only include up to 3 hints
        assert comment.count("> sop_") <= 3


# ── SkillCrystallizer ─────────────────────────────────────────────────────────


class TestSkillCrystallizer:
    def test_crystallize_merged_pr(self, store: InsightStore) -> None:
        crystallizer = SkillCrystallizer(store)
        previous = {
            1: TrackedPR(
                number=1, state=PRTrackingState.FIX_REQUESTED, notes="jest timeout failure"
            )
        }
        current = {
            1: TrackedPR(number=1, state=PRTrackingState.MERGED, notes="jest timeout failure")
        }

        recorded = crystallizer.crystallize_transitions(previous, current)
        assert recorded == 1
        skills = store.all_skills()
        assert len(skills) == 1
        assert skills[0].success_count == 1

    def test_crystallize_escalated_pr(self, store: InsightStore) -> None:
        crystallizer = SkillCrystallizer(store)
        previous = {
            2: TrackedPR(number=2, state=PRTrackingState.FIX_REQUESTED, notes="build failure")
        }
        current = {2: TrackedPR(number=2, state=PRTrackingState.ESCALATED, notes="build failure")}

        recorded = crystallizer.crystallize_transitions(previous, current)
        assert recorded == 1
        skills = store.all_skills()
        assert skills[0].fail_count == 1

    def test_no_transition_not_crystallized(self, store: InsightStore) -> None:
        crystallizer = SkillCrystallizer(store)
        pr = TrackedPR(number=3, state=PRTrackingState.MERGED, notes="some notes")
        recorded = crystallizer.crystallize_transitions({3: pr}, {3: pr})
        assert recorded == 0

    def test_empty_notes_skipped(self, store: InsightStore) -> None:
        crystallizer = SkillCrystallizer(store)
        previous = {4: TrackedPR(number=4, state=PRTrackingState.CI_FAILING)}
        current = {4: TrackedPR(number=4, state=PRTrackingState.MERGED)}
        recorded = crystallizer.crystallize_transitions(previous, current)
        assert recorded == 0


class TestCategoryInference:
    def test_jest_maps_to_ci(self) -> None:
        assert _infer_category("jest timeout in setup file") == CATEGORY_CI

    def test_build_maps_to_build(self) -> None:
        assert _infer_category("tsc compilation failed: import error") == CATEGORY_BUILD

    def test_default_is_ci(self) -> None:
        assert _infer_category("something unknown happened") == CATEGORY_CI


# ── ReflectionEngine ─────────────────────────────────────────────────────────


class TestReflectionEngine:
    def _make_state_with_stale_goal(self, goal_id: str, n: int) -> OrchestratorState:
        state = OrchestratorState()
        state.goal_history[goal_id] = [
            GoalSnapshot(goal_id=goal_id, score=0.5, status=GoalStatus.STALE) for _ in range(n)
        ]
        return state

    def _make_evaluation_with_stale(self, goal_id: str) -> object:
        from caretaker.goals.models import GoalEvaluation

        return GoalEvaluation(
            snapshots={goal_id: GoalSnapshot(goal_id=goal_id, score=0.5, status=GoalStatus.STALE)},
            overall_health=0.5,
        )

    def test_should_reflect_stale_5_runs(self) -> None:
        engine = ReflectionEngine()
        goal_id = "ci_health"
        state = self._make_state_with_stale_goal(goal_id, 5)
        evaluation = self._make_evaluation_with_stale(goal_id)
        assert engine.should_reflect(evaluation, state) is True

    def test_should_not_reflect_stale_3_runs(self) -> None:
        engine = ReflectionEngine()
        goal_id = "ci_health"
        state = self._make_state_with_stale_goal(goal_id, 3)
        evaluation = self._make_evaluation_with_stale(goal_id)
        assert engine.should_reflect(evaluation, state) is False

    def test_should_reflect_diverging_3_runs(self) -> None:
        from caretaker.goals.models import GoalEvaluation

        engine = ReflectionEngine()
        goal_id = "pr_throughput"
        state = OrchestratorState()
        state.goal_history[goal_id] = [
            GoalSnapshot(goal_id=goal_id, score=0.7, status=GoalStatus.DIVERGING),
            GoalSnapshot(goal_id=goal_id, score=0.6, status=GoalStatus.DIVERGING),
            GoalSnapshot(goal_id=goal_id, score=0.5, status=GoalStatus.DIVERGING),
        ]
        evaluation = GoalEvaluation(
            snapshots={
                goal_id: GoalSnapshot(goal_id=goal_id, score=0.4, status=GoalStatus.DIVERGING)
            },
            overall_health=0.4,
        )
        assert engine.should_reflect(evaluation, state) is True

    def test_triggered_goals_returns_correct_ids(self) -> None:
        from caretaker.goals.models import GoalEvaluation

        engine = ReflectionEngine()
        goal_id = "ci_health"
        state = self._make_state_with_stale_goal(goal_id, 6)
        evaluation = GoalEvaluation(
            snapshots={goal_id: GoalSnapshot(goal_id=goal_id, score=0.5, status=GoalStatus.STALE)},
            overall_health=0.5,
        )
        triggered = engine.triggered_goals(evaluation, state)
        assert goal_id in triggered


# ── StrategyMutator ───────────────────────────────────────────────────────────


class TestStrategyMutator:
    def test_max_concurrent_guard(self, store: InsightStore) -> None:
        from caretaker.evolution.mutator import MAX_CONCURRENT

        mutator = StrategyMutator(store)
        # Fill up to MAX_CONCURRENT active mutations
        for i in range(MAX_CONCURRENT):
            m = Mutation(
                id=f"mut-{i}",
                agent_name="pr_agent",
                parameter="copilot_max_retries",
                old_value="2",
                new_value="3",
                goal_id="ci_health",
                goal_score_before=0.4,
                goal_score_after=None,
                runs_evaluated=0,
                started_at=datetime.now(UTC),
                ended_at=None,
                outcome="pending",
            )
            store.upsert_mutation(m)

        from caretaker.evolution.reflection import ReflectionResult, StrategyRecommendation

        reflection = ReflectionResult(
            analysis="test",
            recommendations=[
                StrategyRecommendation("pr_agent", "copilot_max_retries", "4", "test rationale")
            ],
        )
        from caretaker.config import MaintainerConfig

        result = mutator.propose_mutation(reflection, OrchestratorState(), MaintainerConfig())
        assert result is None  # blocked by MAX_CONCURRENT

    def test_mutation_evaluation_acceptance(self, store: InsightStore) -> None:
        from caretaker.evolution.mutator import ACCEPTANCE_DELTA, MIN_RUNS_BEFORE_EVAL
        from caretaker.goals.models import GoalEvaluation

        mutator = StrategyMutator(store)
        m = Mutation(
            id="eval-mut-1",
            agent_name="pr_agent",
            parameter="copilot_max_retries",
            old_value="2",
            new_value="3",
            goal_id="ci_health",
            goal_score_before=0.4,
            goal_score_after=None,
            runs_evaluated=0,
            started_at=datetime.now(UTC),
            ended_at=None,
            outcome="pending",
        )
        store.upsert_mutation(m)

        state = OrchestratorState()
        state.goal_history["ci_health"] = [
            GoalSnapshot(
                goal_id="ci_health",
                score=0.4 + ACCEPTANCE_DELTA + 0.01,
                status=GoalStatus.PROGRESSING,
            )
        ]
        evaluation = GoalEvaluation(
            snapshots={
                "ci_health": GoalSnapshot(
                    goal_id="ci_health", score=0.5, status=GoalStatus.PROGRESSING
                )
            },
            overall_health=0.5,
        )

        # Simulate MIN_RUNS_BEFORE_EVAL-1 runs (not yet evaluatable)
        for _ in range(MIN_RUNS_BEFORE_EVAL - 1):
            outcomes = mutator.evaluate_pending(state, evaluation)
            assert outcomes == []

        # Final run triggers evaluation
        outcomes = mutator.evaluate_pending(state, evaluation)
        assert len(outcomes) == 1
        assert outcomes[0].outcome == "accepted"


# ── TrackedPR fix_cycles ──────────────────────────────────────────────────────


class TestTrackedPREvolutionFields:
    def test_fix_cycles_default_zero(self) -> None:
        pr = TrackedPR(number=1)
        assert pr.fix_cycles == 0
        assert pr.stuck_reflection_done is False
        assert pr.last_state_change_at is None

    def test_fix_cycles_increments(self) -> None:
        pr = TrackedPR(number=1)
        pr.fix_cycles += 1
        assert pr.fix_cycles == 1

    def test_serialization_roundtrip(self) -> None:
        pr = TrackedPR(
            number=42,
            fix_cycles=3,
            stuck_reflection_done=True,
            last_state_change_at=datetime.now(UTC),
        )
        data = pr.model_dump_json()
        restored = TrackedPR.model_validate_json(data)
        assert restored.fix_cycles == 3
        assert restored.stuck_reflection_done is True


# ── OrchestratorState active_plan_ids ─────────────────────────────────────────


class TestOrchestratorStateEvolutionFields:
    def test_active_plan_ids_default_empty(self) -> None:
        state = OrchestratorState()
        assert state.active_plan_ids == {}

    def test_serialization_roundtrip(self) -> None:
        state = OrchestratorState()
        state.active_plan_ids["ci_health"] = 42
        data = state.model_dump_json()
        restored = OrchestratorState.model_validate_json(data)
        assert restored.active_plan_ids == {"ci_health": 42}
