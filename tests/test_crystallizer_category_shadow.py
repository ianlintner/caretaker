"""Tests for T-A10: crystallizer category migration.

Covers the ``@shadow_decision("crystallizer_category")`` dispatch that
routes category inference through the shared
:class:`~caretaker.pr_agent.ci_triage.FailureTriage` classifier:

* ``mode: off`` — legacy :func:`_infer_category` regex ladder is the
  sole authority, behaviour is byte-identical to the pre-T-A10 world.
* ``mode: shadow`` — both paths run, the legacy verdict is returned,
  the disagreement counter records when categories diverge.
* Mapping table — every :data:`FailureCategory` collapses cleanly onto
  one of the InsightStore ``CATEGORY_*`` constants.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator

from caretaker.config import AgenticConfig, AgenticDomainConfig
from caretaker.evolution import shadow_config
from caretaker.evolution.crystallizer import (
    _FAILURE_CATEGORY_TO_STORE,
    SkillCrystallizer,
    _infer_category,
    _map_triage_category,
    _notes_to_check_run,
)
from caretaker.evolution.insight_store import (
    CATEGORY_BUILD,
    CATEGORY_CI,
    CATEGORY_SECURITY,
    InsightStore,
)
from caretaker.evolution.shadow import clear_records_for_tests, recent_records
from caretaker.llm.claude import StructuredCompleteError
from caretaker.pr_agent.ci_triage import FailureTriage
from caretaker.state.models import PRTrackingState, TrackedPR


@pytest.fixture
def store() -> InsightStore:
    return InsightStore(db_path=":memory:")


@pytest.fixture(autouse=True)
def _reset_shadow_state() -> Iterator[None]:
    """Clear ring buffer + active shadow config between tests."""
    clear_records_for_tests()
    shadow_config.reset_for_tests()
    yield
    clear_records_for_tests()
    shadow_config.reset_for_tests()


def _set_mode(mode: str) -> None:
    cfg = AgenticConfig(
        crystallizer_category=AgenticDomainConfig(mode=mode)  # type: ignore[arg-type]
    )
    shadow_config.configure(cfg)


def _make_router(
    verdict: FailureTriage | None = None,
    *,
    error: Exception | None = None,
    available: bool = True,
) -> MagicMock:
    """Build a minimal LLMRouter-shaped mock."""
    claude = MagicMock()
    claude.available = available
    if error is not None:
        claude.structured_complete = AsyncMock(side_effect=error)
    else:
        claude.structured_complete = AsyncMock(return_value=verdict)
    router = MagicMock()
    router.claude_available = available
    router.claude = claude
    return router


# ── Mapping table ────────────────────────────────────────────────────────


class TestFailureCategoryMapping:
    """Every FailureTriage.category value must map onto a CATEGORY_*.

    Shadow-mode comparisons only make sense when the candidate's verdict
    can be rendered in the legacy vocabulary — if a FailureCategory
    silently drops to ``None`` / ``KeyError`` the disagreement counter
    becomes unreadable.
    """

    def test_all_literal_values_covered(self) -> None:
        # The Literal-typed FailureCategory values we expect the LLM to
        # return. Kept in sync with
        # :data:`caretaker.pr_agent.ci_triage.FailureCategory`.
        expected = {
            "test",
            "lint",
            "build",
            "type",
            "timeout",
            "flaky",
            "backpressure",
            "infra",
            "unknown",
        }
        assert set(_FAILURE_CATEGORY_TO_STORE.keys()) == expected

    @pytest.mark.parametrize(
        ("triage_category", "expected_store_category"),
        [
            ("test", CATEGORY_CI),
            ("lint", CATEGORY_CI),
            ("type", CATEGORY_CI),
            ("timeout", CATEGORY_CI),
            ("build", CATEGORY_BUILD),
            ("flaky", CATEGORY_CI),
            ("backpressure", CATEGORY_CI),
            ("infra", CATEGORY_CI),
            ("unknown", CATEGORY_CI),
        ],
    )
    def test_triage_category_maps_to_store_category(
        self, triage_category: str, expected_store_category: str
    ) -> None:
        assert _map_triage_category(triage_category) == expected_store_category  # type: ignore[arg-type]

    def test_legacy_security_has_no_triage_analogue(self) -> None:
        """Legacy regex hits CATEGORY_SECURITY; the LLM schema cannot.

        This is documented in the ``TODO(T-A10, crystallizer_category)``
        note on :data:`_FAILURE_CATEGORY_TO_STORE`. Until the
        :data:`FailureCategory` Literal grows a ``security`` row, the
        candidate will route security-flavoured notes to ``CATEGORY_CI``
        and the shadow disagreement counter will surface how often.
        """
        assert _infer_category("dependabot cve patch for snyk alert") == CATEGORY_SECURITY
        assert CATEGORY_SECURITY not in set(_FAILURE_CATEGORY_TO_STORE.values())


# ── Synthetic CheckRun wrapper ────────────────────────────────────────────


class TestNotesToCheckRun:
    def test_wraps_notes_as_output_summary(self) -> None:
        cr = _notes_to_check_run("jest timeout after 30s")
        assert cr.output_summary == "jest timeout after 30s"
        assert cr.name == "crystallizer"

    def test_empty_notes_still_valid(self) -> None:
        # Crystallizer callers always skip empty notes (see
        # :meth:`SkillCrystallizer.crystallize_transitions`) but the
        # adapter should not care — a valid CheckRun is all we owe the
        # classifier.
        cr = _notes_to_check_run("")
        assert cr.output_summary == ""


# ── mode: off ─────────────────────────────────────────────────────────────


class TestOffMode:
    """``mode: off`` — legacy path only, byte-identical to pre-T-A10.

    Critically the LLM router must never be called — no prompt budget,
    no provider round-trips, no Prometheus labels beyond ``legacy_only``.
    """

    @pytest.mark.asyncio
    async def test_legacy_path_still_works_without_router(self, store: InsightStore) -> None:
        _set_mode("off")
        crystallizer = SkillCrystallizer(store, llm_router=None)
        previous = {
            1: TrackedPR(
                number=1, state=PRTrackingState.FIX_REQUESTED, notes="jest timeout failure"
            )
        }
        current = {
            1: TrackedPR(number=1, state=PRTrackingState.MERGED, notes="jest timeout failure")
        }

        recorded = await crystallizer.crystallize_transitions(previous, current)

        assert recorded == 1
        skills = store.all_skills(CATEGORY_CI)
        assert len(skills) == 1
        # Off-mode produces no shadow records.
        assert recent_records(name="crystallizer_category") == []

    @pytest.mark.asyncio
    async def test_candidate_never_invoked_in_off_mode(self, store: InsightStore) -> None:
        _set_mode("off")
        # Even if a router is wired, ``off`` must never reach for Claude.
        router = _make_router(
            verdict=FailureTriage(
                category="build",  # would disagree with legacy jest->CI
                confidence=0.9,
                is_transient=False,
                root_cause_hypothesis="LLM says build",
                suggested_fix="N/A",
            )
        )
        crystallizer = SkillCrystallizer(store, llm_router=router)

        previous = {
            1: TrackedPR(
                number=1, state=PRTrackingState.FIX_REQUESTED, notes="jest timeout failure"
            )
        }
        current = {
            1: TrackedPR(number=1, state=PRTrackingState.MERGED, notes="jest timeout failure")
        }
        await crystallizer.crystallize_transitions(previous, current)

        # Legacy category wins and the LLM was never consulted.
        assert router.claude.structured_complete.await_count == 0
        assert len(store.all_skills(CATEGORY_CI)) == 1
        assert len(store.all_skills(CATEGORY_BUILD)) == 0


# ── mode: shadow ──────────────────────────────────────────────────────────


class TestShadowMode:
    """``mode: shadow`` — both paths run, legacy verdict returned.

    Disagreement records let operators inspect the LLM vs regex
    divergence rate before flipping ``enforce``.
    """

    @pytest.mark.asyncio
    async def test_agreement_records_agree_outcome(self, store: InsightStore) -> None:
        _set_mode("shadow")
        # Legacy: "jest" → CATEGORY_CI. LLM: "test" → CATEGORY_CI. Agree.
        router = _make_router(
            verdict=FailureTriage(
                category="test",
                confidence=0.9,
                is_transient=False,
                root_cause_hypothesis="jest assertion failed",
                suggested_fix="Fix the assertion.",
            )
        )
        crystallizer = SkillCrystallizer(store, llm_router=router)

        previous = {
            1: TrackedPR(
                number=1, state=PRTrackingState.FIX_REQUESTED, notes="jest timeout failure"
            )
        }
        current = {
            1: TrackedPR(number=1, state=PRTrackingState.MERGED, notes="jest timeout failure")
        }
        recorded = await crystallizer.crystallize_transitions(
            previous, current, repo_slug="ian/demo"
        )

        assert recorded == 1
        # Legacy verdict wins in shadow mode.
        assert len(store.all_skills(CATEGORY_CI)) == 1

        records = recent_records(name="crystallizer_category")
        assert len(records) == 1
        rec = records[0]
        assert rec.outcome == "agree"
        assert rec.mode == "shadow"
        assert rec.repo_slug == "ian/demo"

    @pytest.mark.asyncio
    async def test_disagreement_records_disagree_outcome(self, store: InsightStore) -> None:
        _set_mode("shadow")
        # Legacy: "jest" → CATEGORY_CI. LLM: "build" → CATEGORY_BUILD.
        # Disagreement is captured but the legacy verdict is what
        # reaches the InsightStore.
        router = _make_router(
            verdict=FailureTriage(
                category="build",
                confidence=0.8,
                is_transient=False,
                root_cause_hypothesis="webpack emitted missing chunk",
                suggested_fix="Rebuild the bundle.",
            )
        )
        crystallizer = SkillCrystallizer(store, llm_router=router)

        previous = {
            1: TrackedPR(number=1, state=PRTrackingState.FIX_REQUESTED, notes="jest flake in setup")
        }
        current = {
            1: TrackedPR(number=1, state=PRTrackingState.MERGED, notes="jest flake in setup")
        }
        await crystallizer.crystallize_transitions(previous, current)

        # Legacy wins: the skill is recorded under CATEGORY_CI, not BUILD.
        assert len(store.all_skills(CATEGORY_CI)) == 1
        assert len(store.all_skills(CATEGORY_BUILD)) == 0

        records = recent_records(name="crystallizer_category")
        assert len(records) == 1
        rec = records[0]
        assert rec.outcome == "disagree"
        assert rec.mode == "shadow"
        assert rec.disagreement_reason is not None

    @pytest.mark.asyncio
    async def test_candidate_error_does_not_break_hot_path(self, store: InsightStore) -> None:
        _set_mode("shadow")
        # The shared classifier swallows StructuredCompleteError and
        # returns None; the shadow decorator then compares ``None`` vs
        # the legacy string verdict → disagreement record.
        router = _make_router(
            error=StructuredCompleteError(
                raw_text="not json",
                validation_error=ValueError("parse failed"),
            )
        )
        crystallizer = SkillCrystallizer(store, llm_router=router)

        previous = {
            1: TrackedPR(
                number=1, state=PRTrackingState.FIX_REQUESTED, notes="jest timeout failure"
            )
        }
        current = {
            1: TrackedPR(number=1, state=PRTrackingState.MERGED, notes="jest timeout failure")
        }
        # Hot path stays green.
        recorded = await crystallizer.crystallize_transitions(previous, current)
        assert recorded == 1
        assert len(store.all_skills(CATEGORY_CI)) == 1

        records = recent_records(name="crystallizer_category")
        assert len(records) == 1

    @pytest.mark.asyncio
    async def test_shadow_without_router_runs_legacy_only(self, store: InsightStore) -> None:
        """Shadow + no LLM router → candidate returns None → disagreement.

        Keeps the shadow-mode contract honest even when Claude is not
        wired up: the hot path still resolves to the legacy verdict.
        """
        _set_mode("shadow")
        crystallizer = SkillCrystallizer(store, llm_router=None)

        previous = {
            1: TrackedPR(
                number=1, state=PRTrackingState.FIX_REQUESTED, notes="build failure in tsc"
            )
        }
        current = {
            1: TrackedPR(number=1, state=PRTrackingState.MERGED, notes="build failure in tsc")
        }
        recorded = await crystallizer.crystallize_transitions(previous, current)
        assert recorded == 1
        # Legacy: "build" in notes → CATEGORY_BUILD.
        assert len(store.all_skills(CATEGORY_BUILD)) == 1


# ── mode: enforce ─────────────────────────────────────────────────────────


class TestEnforceMode:
    """``mode: enforce`` — candidate is authoritative.

    Smoke-test only; the bulk of the enforce-mode state-machine is
    covered by the shared :func:`shadow_decision` tests.
    """

    @pytest.mark.asyncio
    async def test_enforce_uses_candidate_category(self, store: InsightStore) -> None:
        _set_mode("enforce")
        # Legacy would classify "jest timeout" as CATEGORY_CI. The LLM
        # says "build" → CATEGORY_BUILD. In enforce mode the candidate
        # wins.
        router = _make_router(
            verdict=FailureTriage(
                category="build",
                confidence=0.95,
                is_transient=False,
                root_cause_hypothesis="test setup touches a webpack artifact",
                suggested_fix="Pin the bundle before running tests.",
            )
        )
        crystallizer = SkillCrystallizer(store, llm_router=router)

        previous = {
            1: TrackedPR(
                number=1, state=PRTrackingState.FIX_REQUESTED, notes="jest timeout failure"
            )
        }
        current = {
            1: TrackedPR(number=1, state=PRTrackingState.MERGED, notes="jest timeout failure")
        }
        await crystallizer.crystallize_transitions(previous, current)

        # Enforce → candidate wins.
        assert len(store.all_skills(CATEGORY_BUILD)) == 1
        assert len(store.all_skills(CATEGORY_CI)) == 0

    @pytest.mark.asyncio
    async def test_enforce_falls_through_to_legacy_on_candidate_none(
        self, store: InsightStore
    ) -> None:
        _set_mode("enforce")
        # Candidate returns None (no router) → decorator falls through
        # to the legacy verdict.
        crystallizer = SkillCrystallizer(store, llm_router=None)

        previous = {
            1: TrackedPR(
                number=1, state=PRTrackingState.FIX_REQUESTED, notes="jest timeout failure"
            )
        }
        current = {
            1: TrackedPR(number=1, state=PRTrackingState.MERGED, notes="jest timeout failure")
        }
        await crystallizer.crystallize_transitions(previous, current)

        # Legacy: "jest" → CATEGORY_CI.
        assert len(store.all_skills(CATEGORY_CI)) == 1
