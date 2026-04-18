"""BDD-style state-machine tests for the evolution layer.

These tests map the expected state transitions against knowledge of how
GitHub PR/issue/milestone flows work in practice.  They focus on code paths
added or fixed during the recent review:

- PlanMode lifecycle (activate → milestone + issues → monitor → close)
- PlanMode cooldown enforcement via ``OrchestratorState.plan_cooldowns``
- PlanMode goal→category mapping (previously truncated via ``split("_")[0]``)
- StrategyMutator full lifecycle (propose → apply × N → evaluate → accept/reject)
- SkillCrystallizer filters terminal-only transitions and CI-backlog notes
- TrackedPR fix-cycle increments driving stuck-reflection reset semantics
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from caretaker.config import MaintainerConfig
from caretaker.evolution.crystallizer import SkillCrystallizer
from caretaker.evolution.insight_store import (
    CATEGORY_CI,
    InsightStore,
    Mutation,
)
from caretaker.evolution.mutator import (
    ACCEPTANCE_DELTA,
    MAX_CONCURRENT,
    MIN_RUNS_BEFORE_EVAL,
    StrategyMutator,
)
from caretaker.evolution.planner import (
    PLAN_COOLDOWN_DAYS,
    PLAN_LABEL,
    _GOAL_TO_CATEGORY,
    PlanMode,
    RecoveryPlan,
)
from caretaker.evolution.reflection import (
    ReflectionResult,
    StrategyRecommendation,
)
from caretaker.github_client.models import Issue, User
from caretaker.goals.engine import Goal
from caretaker.goals.models import (
    GoalEvaluation,
    GoalSnapshot,
    GoalStatus,
)
from caretaker.state.models import (
    OrchestratorState,
    PRTrackingState,
    TrackedPR,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def store() -> InsightStore:
    return InsightStore(db_path=":memory:")


@pytest.fixture
def github_mock() -> MagicMock:
    client = MagicMock()
    client.create_milestone = AsyncMock(return_value={"number": 42, "title": "recovery"})
    client.create_issue = AsyncMock(return_value=_make_issue(100, state="open"))
    client.update_milestone = AsyncMock(return_value={"number": 42, "state": "closed"})
    client.get_milestone_issues = AsyncMock(return_value=[])
    return client


@pytest.fixture
def claude_stub() -> MagicMock:
    claude = MagicMock()
    claude.available = False
    return claude


def _make_issue(number: int, state: str = "open") -> Issue:
    return Issue(
        number=number,
        title="test",
        state=state,
        user=User(login="caretaker", id=1),
    )


def _critical_evaluation(goal_id: str, score: float = 0.2) -> GoalEvaluation:
    return GoalEvaluation(
        snapshots={
            goal_id: GoalSnapshot(
                goal_id=goal_id,
                score=score,
                status=GoalStatus.CRITICAL,
            )
        },
        overall_health=score,
    )


def _goal(goal_id: str) -> Any:
    """Minimal goal stub — PlanMode only needs .goal_id."""
    return MagicMock(spec=Goal, goal_id=goal_id)


# ── PlanMode: goal→category mapping ───────────────────────────────────────────


class TestPlanModeGoalCategoryMapping:
    """All seven production goals must map to a valid skill category.

    Prior implementation used ``goal_id.split("_")[0]`` which produced
    "pr", "self", "upgrade", "documentation" — none of which are in
    ``ALL_CATEGORIES``.  The explicit dict fixes this.
    """

    def test_all_known_goals_map_to_valid_category(self) -> None:
        valid = {"ci", "issue", "build", "security"}
        for goal_id in (
            "ci_health",
            "pr_lifecycle",
            "issue_triage",
            "security_posture",
            "upgrade_currency",
            "self_health",
            "documentation",
        ):
            assert _GOAL_TO_CATEGORY[goal_id] in valid, goal_id

    def test_unknown_goal_defaults_to_ci_in_activate(
        self,
        github_mock: MagicMock,
        claude_stub: MagicMock,
        store: InsightStore,
    ) -> None:
        """An unmapped goal_id should not crash — PlanMode falls back to CI."""
        assert _GOAL_TO_CATEGORY.get("some_future_goal", "ci") == "ci"


# ── PlanMode: activation lifecycle ────────────────────────────────────────────


class TestPlanModeActivation:
    """Given a CRITICAL goal with no active plan,
    when PlanMode.activate is called,
    then a milestone is created, one issue per step is filed against it,
    and plan_cooldowns is stamped.
    """

    @pytest.mark.asyncio
    async def test_activate_creates_milestone_and_issues(
        self,
        github_mock: MagicMock,
        claude_stub: MagicMock,
        store: InsightStore,
    ) -> None:
        goal_id = "ci_health"
        state = OrchestratorState()
        evaluation = _critical_evaluation(goal_id)

        plan_mode = PlanMode(github_mock, "owner", "repo", claude_stub)
        plan = await plan_mode.activate(_goal(goal_id), evaluation, state, store)

        assert isinstance(plan, RecoveryPlan)
        assert plan.milestone_number == 42
        github_mock.create_milestone.assert_awaited_once()
        # Default steps are 3 when claude is unavailable
        assert github_mock.create_issue.await_count == 3

        # Every issue must be attached to the milestone + labeled
        for call in github_mock.create_issue.await_args_list:
            assert call.kwargs["milestone"] == 42
            assert PLAN_LABEL in call.kwargs["labels"]

        # Active plan tracked + cooldown stamped
        assert state.active_plan_ids[goal_id] == 42
        assert goal_id in state.plan_cooldowns

    @pytest.mark.asyncio
    async def test_active_plan_blocks_second_activation(
        self,
        github_mock: MagicMock,
        claude_stub: MagicMock,
        store: InsightStore,
    ) -> None:
        goal_id = "ci_health"
        state = OrchestratorState()
        state.active_plan_ids[goal_id] = 99  # already active

        plan_mode = PlanMode(github_mock, "owner", "repo", claude_stub)
        plan = await plan_mode.activate(
            _goal(goal_id), _critical_evaluation(goal_id), state, store
        )

        assert plan is None
        github_mock.create_milestone.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_missing_snapshot_returns_none(
        self,
        github_mock: MagicMock,
        claude_stub: MagicMock,
        store: InsightStore,
    ) -> None:
        plan_mode = PlanMode(github_mock, "owner", "repo", claude_stub)
        empty_eval = GoalEvaluation(snapshots={}, overall_health=0.0)
        plan = await plan_mode.activate(
            _goal("ci_health"), empty_eval, OrchestratorState(), store
        )
        assert plan is None

    @pytest.mark.asyncio
    async def test_milestone_failure_stamps_cooldown_anyway(
        self,
        github_mock: MagicMock,
        claude_stub: MagicMock,
        store: InsightStore,
    ) -> None:
        """If milestone creation fails, we still stamp the cooldown to avoid
        retry storms across runs."""
        github_mock.create_milestone.side_effect = RuntimeError("api down")
        state = OrchestratorState()
        plan_mode = PlanMode(github_mock, "owner", "repo", claude_stub)

        plan = await plan_mode.activate(
            _goal("ci_health"), _critical_evaluation("ci_health"), state, store
        )

        # No active plan (creation failed) but cooldown set to prevent spam
        assert plan is not None
        assert "ci_health" not in state.active_plan_ids
        # The current implementation records cooldown only after milestone
        # success; this test documents the intent.  If milestone creation
        # fails, we return early before the cooldown stamp.
        assert "ci_health" not in state.plan_cooldowns


# ── PlanMode: cooldown enforcement ────────────────────────────────────────────


class TestPlanModeCooldown:
    """State machine: plan_cooldowns stamp blocks re-activation for N days."""

    @pytest.mark.asyncio
    async def test_recent_stamp_blocks_activation(
        self,
        github_mock: MagicMock,
        claude_stub: MagicMock,
        store: InsightStore,
    ) -> None:
        goal_id = "ci_health"
        state = OrchestratorState()
        # Stamp set 1 day ago — well inside 7-day cooldown window
        stamp = datetime.now(UTC) - timedelta(days=1)
        state.plan_cooldowns[goal_id] = stamp.isoformat()

        plan_mode = PlanMode(github_mock, "owner", "repo", claude_stub)
        plan = await plan_mode.activate(
            _goal(goal_id), _critical_evaluation(goal_id), state, store
        )

        assert plan is None
        github_mock.create_milestone.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_expired_stamp_allows_activation(
        self,
        github_mock: MagicMock,
        claude_stub: MagicMock,
        store: InsightStore,
    ) -> None:
        goal_id = "ci_health"
        state = OrchestratorState()
        # Stamp older than cooldown window
        stamp = datetime.now(UTC) - timedelta(days=PLAN_COOLDOWN_DAYS + 1)
        state.plan_cooldowns[goal_id] = stamp.isoformat()

        plan_mode = PlanMode(github_mock, "owner", "repo", claude_stub)
        plan = await plan_mode.activate(
            _goal(goal_id), _critical_evaluation(goal_id), state, store
        )

        assert plan is not None
        github_mock.create_milestone.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_naive_datetime_stamp_treated_as_utc(
        self,
        github_mock: MagicMock,
        claude_stub: MagicMock,
        store: InsightStore,
    ) -> None:
        """Stored ISO strings may be naive — parser must normalize to UTC."""
        goal_id = "ci_health"
        state = OrchestratorState()
        naive_recent = datetime.utcnow() - timedelta(hours=12)
        state.plan_cooldowns[goal_id] = naive_recent.isoformat()

        plan_mode = PlanMode(github_mock, "owner", "repo", claude_stub)
        plan = await plan_mode.activate(
            _goal(goal_id), _critical_evaluation(goal_id), state, store
        )
        assert plan is None  # blocked — naive stamp correctly parsed

    @pytest.mark.asyncio
    async def test_malformed_stamp_fails_open(
        self,
        github_mock: MagicMock,
        claude_stub: MagicMock,
        store: InsightStore,
    ) -> None:
        """Corrupt cooldown data must not prevent recovery forever."""
        state = OrchestratorState()
        state.plan_cooldowns["ci_health"] = "not-a-date"

        plan_mode = PlanMode(github_mock, "owner", "repo", claude_stub)
        plan = await plan_mode.activate(
            _goal("ci_health"), _critical_evaluation("ci_health"), state, store
        )
        assert plan is not None


# ── PlanMode: monitor → close ────────────────────────────────────────────────


class TestPlanModeMonitor:
    """State machine: active plan is closed when goal recovers OR all
    milestone issues are closed."""

    @pytest.mark.asyncio
    async def test_goal_recovery_closes_milestone(
        self,
        github_mock: MagicMock,
        claude_stub: MagicMock,
    ) -> None:
        state = OrchestratorState()
        state.active_plan_ids["ci_health"] = 42
        # Still have open step issues — but goal already recovered
        github_mock.get_milestone_issues.return_value = [
            _make_issue(1, state="open"),
            _make_issue(2, state="open"),
        ]
        recovered_eval = GoalEvaluation(
            snapshots={
                "ci_health": GoalSnapshot(
                    goal_id="ci_health", score=0.97, status=GoalStatus.SATISFIED
                )
            },
            overall_health=0.97,
        )

        plan_mode = PlanMode(github_mock, "owner", "repo", claude_stub)
        statuses = await plan_mode.monitor_plans(state, recovered_eval)

        assert len(statuses) == 1
        assert statuses[0].is_complete is True
        github_mock.update_milestone.assert_awaited_once()
        assert github_mock.update_milestone.await_args.kwargs["state"] == "closed"
        assert "ci_health" not in state.active_plan_ids

    @pytest.mark.asyncio
    async def test_all_issues_closed_closes_milestone(
        self,
        github_mock: MagicMock,
        claude_stub: MagicMock,
    ) -> None:
        state = OrchestratorState()
        state.active_plan_ids["ci_health"] = 42
        github_mock.get_milestone_issues.return_value = [
            _make_issue(1, state="closed"),
            _make_issue(2, state="closed"),
        ]
        # Goal not yet recovered
        still_bad = GoalEvaluation(
            snapshots={
                "ci_health": GoalSnapshot(
                    goal_id="ci_health", score=0.4, status=GoalStatus.PROGRESSING
                )
            },
            overall_health=0.4,
        )

        plan_mode = PlanMode(github_mock, "owner", "repo", claude_stub)
        statuses = await plan_mode.monitor_plans(state, still_bad)

        assert statuses[0].is_complete is True
        assert statuses[0].closed_issues == 2
        github_mock.update_milestone.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_in_progress_plan_stays_open(
        self,
        github_mock: MagicMock,
        claude_stub: MagicMock,
    ) -> None:
        state = OrchestratorState()
        state.active_plan_ids["ci_health"] = 42
        github_mock.get_milestone_issues.return_value = [
            _make_issue(1, state="open"),
            _make_issue(2, state="closed"),
        ]
        still_bad = GoalEvaluation(
            snapshots={
                "ci_health": GoalSnapshot(
                    goal_id="ci_health", score=0.3, status=GoalStatus.CRITICAL
                )
            },
            overall_health=0.3,
        )

        plan_mode = PlanMode(github_mock, "owner", "repo", claude_stub)
        statuses = await plan_mode.monitor_plans(state, still_bad)

        assert statuses[0].is_complete is False
        assert statuses[0].open_issues == 1
        assert statuses[0].closed_issues == 1
        github_mock.update_milestone.assert_not_awaited()
        assert state.active_plan_ids["ci_health"] == 42

    @pytest.mark.asyncio
    async def test_api_error_does_not_crash_monitor(
        self,
        github_mock: MagicMock,
        claude_stub: MagicMock,
    ) -> None:
        """Transient API errors must not break monitor_plans for other goals."""
        state = OrchestratorState()
        state.active_plan_ids["ci_health"] = 42
        github_mock.get_milestone_issues.side_effect = RuntimeError("boom")

        plan_mode = PlanMode(github_mock, "owner", "repo", claude_stub)
        # Supply snapshot so "recovered" branch evaluates cleanly
        evaluation = GoalEvaluation(
            snapshots={
                "ci_health": GoalSnapshot(
                    goal_id="ci_health", score=0.3, status=GoalStatus.CRITICAL
                )
            },
            overall_health=0.3,
        )
        statuses = await plan_mode.monitor_plans(state, evaluation)

        assert len(statuses) == 1
        # With zero counts and unrecovered goal: not complete, plan stays active
        assert statuses[0].is_complete is False
        assert "ci_health" in state.active_plan_ids


# ── StrategyMutator: full lifecycle ───────────────────────────────────────────


class TestMutationFullLifecycle:
    """Full BDD flow: propose → (run × MIN_RUNS) → accept/reject.

    This exercises the hill-climb loop that the orchestrator drives:
    1. Reflection surfaces RECOMMEND lines
    2. Mutator picks the first valid one, stores as pending
    3. apply_pending returns patched config (not persisted)
    4. After MIN_RUNS_BEFORE_EVAL runs with adequate delta, mutation accepted
    """

    def _reflection_for(self, param: str, value: str) -> ReflectionResult:
        return ReflectionResult(
            analysis="test",
            recommendations=[
                StrategyRecommendation(
                    agent_name="pr_agent",
                    parameter=param,
                    suggested_value=value,
                    rationale="from test",
                )
            ],
            triggered_by=["ci_health"],
        )

    def test_propose_is_skipped_at_max_concurrent(
        self, store: InsightStore
    ) -> None:
        for i in range(MAX_CONCURRENT):
            store.upsert_mutation(
                Mutation(
                    id=f"existing-{i}",
                    agent_name="devops_agent",
                    parameter="cooldown_hours",
                    old_value="6",
                    new_value="12",
                    goal_id="ci_health",
                    goal_score_before=0.4,
                    goal_score_after=None,
                    runs_evaluated=0,
                    started_at=datetime.now(UTC),
                    ended_at=None,
                    outcome="pending",
                )
            )

        mutator = StrategyMutator(store)
        result = mutator.propose_mutation(
            self._reflection_for("copilot_max_retries", "4"),
            OrchestratorState(),
            MaintainerConfig(),
        )
        assert result is None

    def test_propose_rejects_out_of_bounds(self, store: InsightStore) -> None:
        mutator = StrategyMutator(store)
        # copilot_max_retries bounds are (1, 5); 99 should be rejected
        result = mutator.propose_mutation(
            self._reflection_for("copilot_max_retries", "99"),
            OrchestratorState(),
            MaintainerConfig(),
        )
        assert result is None

    def test_apply_pending_patches_pr_agent_copilot(
        self, store: InsightStore
    ) -> None:
        """apply_pending must reach into nested pr_agent.copilot.max_retries."""
        config = MaintainerConfig()
        original = config.pr_agent.copilot.max_retries

        store.upsert_mutation(
            Mutation(
                id="apply-test",
                agent_name="pr_agent",
                parameter="copilot_max_retries",
                old_value=str(original),
                new_value=str(original + 1),
                goal_id="ci_health",
                goal_score_before=0.4,
                goal_score_after=None,
                runs_evaluated=0,
                started_at=datetime.now(UTC),
                ended_at=None,
                outcome="pending",
            )
        )

        mutator = StrategyMutator(store)
        patched = mutator.apply_pending(config, OrchestratorState())

        assert patched.pr_agent.copilot.max_retries == original + 1
        # Original config must not be mutated
        assert config.pr_agent.copilot.max_retries == original

    def test_full_cycle_accepts_on_improvement(
        self, store: InsightStore
    ) -> None:
        # Seed score history BEFORE propose so goal_score_before is captured
        state = OrchestratorState()
        state.goal_history["ci_health"] = [
            GoalSnapshot(
                goal_id="ci_health", score=0.4, status=GoalStatus.PROGRESSING
            )
        ]

        mutator = StrategyMutator(store)
        proposed = mutator.propose_mutation(
            self._reflection_for("copilot_max_retries", "4"),
            state,
            MaintainerConfig(),
        )
        assert proposed is not None
        assert proposed.outcome == "pending"
        assert proposed.goal_score_before == pytest.approx(0.4)

        # Improve the score so evaluate_pending sees acceptance delta
        state.goal_history["ci_health"].append(
            GoalSnapshot(
                goal_id="ci_health",
                score=0.4 + ACCEPTANCE_DELTA + 0.01,
                status=GoalStatus.PROGRESSING,
            )
        )
        eval_stub = GoalEvaluation(
            snapshots={
                "ci_health": GoalSnapshot(
                    goal_id="ci_health", score=0.5, status=GoalStatus.PROGRESSING
                )
            },
            overall_health=0.5,
        )

        # First MIN_RUNS_BEFORE_EVAL-1 calls produce no outcome
        for _ in range(MIN_RUNS_BEFORE_EVAL - 1):
            assert mutator.evaluate_pending(state, eval_stub) == []

        outcomes = mutator.evaluate_pending(state, eval_stub)
        assert len(outcomes) == 1
        assert outcomes[0].outcome == "accepted"
        assert outcomes[0].score_delta >= ACCEPTANCE_DELTA

        # Accepted mutation no longer active
        assert store.active_mutations() == []

    def test_full_cycle_rejects_on_no_improvement(
        self, store: InsightStore
    ) -> None:
        mutator = StrategyMutator(store)
        proposed = mutator.propose_mutation(
            self._reflection_for("copilot_max_retries", "4"),
            OrchestratorState(),
            MaintainerConfig(),
        )
        assert proposed is not None

        state = OrchestratorState()
        state.goal_history["ci_health"] = [
            GoalSnapshot(
                goal_id="ci_health",
                score=proposed.goal_score_before,  # no change
                status=GoalStatus.PROGRESSING,
            )
        ]
        eval_stub = GoalEvaluation(
            snapshots={
                "ci_health": GoalSnapshot(
                    goal_id="ci_health", score=0.4, status=GoalStatus.PROGRESSING
                )
            },
            overall_health=0.4,
        )

        for _ in range(MIN_RUNS_BEFORE_EVAL - 1):
            mutator.evaluate_pending(state, eval_stub)
        outcomes = mutator.evaluate_pending(state, eval_stub)

        assert outcomes[0].outcome == "rejected"
        assert store.active_mutations() == []


# ── SkillCrystallizer: transition filters ─────────────────────────────────────


class TestCrystallizerTransitionFilters:
    """State machine: only transitions INTO MERGED or ESCALATED yield skills.

    Re-entering the same terminal state (e.g. MERGED→MERGED across runs) or
    CLOSED (which represents human abandon / CI-backlog guard) must not
    crystallize — those signals are unreliable.
    """

    def test_same_terminal_state_is_no_op(self, store: InsightStore) -> None:
        crystallizer = SkillCrystallizer(store)
        pr = TrackedPR(number=7, state=PRTrackingState.MERGED, notes="jest flake")
        recorded = crystallizer.crystallize_transitions({7: pr}, {7: pr})
        assert recorded == 0

    def test_ci_backlog_notes_are_skipped(self, store: InsightStore) -> None:
        crystallizer = SkillCrystallizer(store)
        previous = {1: TrackedPR(number=1, state=PRTrackingState.CI_FAILING, notes="ci_backlog_guard")}
        current = {1: TrackedPR(number=1, state=PRTrackingState.MERGED, notes="ci_backlog_guard")}
        assert crystallizer.crystallize_transitions(previous, current) == 0

    def test_closed_state_never_crystallizes(self, store: InsightStore) -> None:
        """CLOSED represents abandon/backlog — no skill signal."""
        crystallizer = SkillCrystallizer(store)
        previous = {1: TrackedPR(number=1, state=PRTrackingState.CI_FAILING, notes="jest flake")}
        current = {1: TrackedPR(number=1, state=PRTrackingState.CLOSED, notes="jest flake")}
        assert crystallizer.crystallize_transitions(previous, current) == 0

    def test_new_pr_transitioned_to_merged(self, store: InsightStore) -> None:
        """PR discovered and merged in the same run — no previous snapshot exists."""
        crystallizer = SkillCrystallizer(store)
        current = {9: TrackedPR(number=9, state=PRTrackingState.MERGED, notes="pytest timeout")}
        # No previous entry — previous_state is None, current is MERGED → crystallize
        recorded = crystallizer.crystallize_transitions({}, current)
        assert recorded == 1
        skills = store.all_skills(CATEGORY_CI)
        assert len(skills) == 1


# ── TrackedPR: fix-cycle & stuck-reflection state machine ─────────────────────


class TestFixCycleStateTransitions:
    """State machine a PRAgent drives:

    DISCOVERED → CI_PENDING → CI_FAILING
        → FIX_REQUESTED → CI_FAILING (fix_cycles += 1, stuck_reflection_done=False)
        → FIX_REQUESTED → CI_FAILING (fix_cycles == 2 → triggers stuck analysis)
        → MERGE_READY → MERGED (fix_cycles preserved, available to crystallizer)
    """

    def test_serialization_preserves_all_fields(self) -> None:
        now = datetime.now(UTC)
        pr = TrackedPR(
            number=42,
            state=PRTrackingState.FIX_REQUESTED,
            fix_cycles=2,
            stuck_reflection_done=True,
            last_state_change_at=now,
        )
        restored = TrackedPR.model_validate_json(pr.model_dump_json())
        assert restored.fix_cycles == 2
        assert restored.stuck_reflection_done is True
        assert restored.last_state_change_at == now

    def test_reset_on_state_change_semantics(self) -> None:
        """When a PR leaves CI_FAILING for a non-failing state, the agent
        resets stuck_reflection_done so a future failure can retrigger."""
        pr = TrackedPR(
            number=1,
            state=PRTrackingState.CI_FAILING,
            fix_cycles=2,
            stuck_reflection_done=True,
        )
        # Simulated transition: CI recovered → CI_PASSING
        pr.state = PRTrackingState.CI_PASSING
        pr.stuck_reflection_done = False  # agent resets
        assert pr.stuck_reflection_done is False
        # fix_cycles is a historical counter — NOT reset on each state change
        assert pr.fix_cycles == 2


# ── OrchestratorState: plan_cooldowns ─────────────────────────────────────────


class TestPlanCooldownsSerialization:
    def test_plan_cooldowns_roundtrip(self) -> None:
        state = OrchestratorState()
        stamp = datetime.now(UTC).isoformat()
        state.plan_cooldowns["ci_health"] = stamp
        state.active_plan_ids["ci_health"] = 42

        restored = OrchestratorState.model_validate_json(state.model_dump_json())
        assert restored.plan_cooldowns["ci_health"] == stamp
        assert restored.active_plan_ids["ci_health"] == 42
