"""Tests for the Phase 2 LLM-backed PR readiness migration.

Covers every call-out in T-A1 Part D:

* Schema validation (happy path + missing-field rejection).
* Legacy adapter mapping: each legacy token -> correct ``Blocker.category``.
* LLM candidate: prompt payload + return value on happy path.
* Candidate error handling: ``StructuredCompleteError`` -> ``None``.
* Status-comment rendering for ``ready`` / ``blocked`` / ``needs_human``.
* Integration through the shadow decorator in all three modes.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from caretaker.config import AgenticConfig, AgenticDomainConfig
from caretaker.evolution import shadow_config
from caretaker.evolution.shadow import (
    clear_records_for_tests,
    recent_records,
    shadow_decision,
)
from caretaker.github_client.models import CheckConclusion, CheckStatus, Label, ReviewState
from caretaker.graph import writer as graph_writer
from caretaker.llm.claude import StructuredCompleteError
from caretaker.pr_agent.ownership import (
    build_status_comment,
    get_readiness_check_summary,
    get_readiness_check_title,
)
from caretaker.pr_agent.readiness_llm import (
    PRReadinessContext,
    Readiness,
    build_readiness_prompt,
    evaluate_pr_readiness_llm,
    readiness_from_legacy,
)
from caretaker.pr_agent.states import (
    evaluate_ci,
    evaluate_readiness,
    evaluate_reviews,
)
from caretaker.state.models import PRTrackingState, TrackedPR
from tests.conftest import make_check_run, make_pr, make_review

# ── Fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_shadow_state() -> None:
    clear_records_for_tests()
    shadow_config.reset_for_tests()
    graph_writer.reset_for_tests()


def _set_readiness_mode(mode: str) -> None:
    cfg = AgenticConfig(readiness=AgenticDomainConfig(mode=mode))  # type: ignore[arg-type]
    shadow_config.configure(cfg)


# ── Part D.1: Schema validation ──────────────────────────────────────────


class TestReadinessSchema:
    def test_happy_path_parses(self) -> None:
        r = Readiness(
            verdict="ready",
            confidence=0.9,
            blockers=[],
            summary="All green.",
        )
        assert r.verdict == "ready"
        assert r.confidence == 0.9
        assert r.summary == "All green."

    def test_missing_required_field_raises(self) -> None:
        with pytest.raises(ValidationError):
            Readiness(confidence=0.5, summary="x")  # type: ignore[call-arg]

    def test_confidence_out_of_range_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Readiness(verdict="ready", confidence=1.5, summary="x")

    def test_summary_length_capped(self) -> None:
        with pytest.raises(ValidationError):
            Readiness(verdict="ready", confidence=0.5, summary="x" * 201)

    def test_blocker_category_closed_enum(self) -> None:
        # ``other`` is valid; bogus categories are rejected.
        with pytest.raises(ValidationError):
            Readiness(
                verdict="blocked",
                confidence=0.5,
                summary="x",
                blockers=[{"category": "nonsense", "human_reason": "r", "suggested_action": "a"}],
            )


# ── Part D.2: Legacy adapter mapping ─────────────────────────────────────


class TestReadinessFromLegacy:
    def test_success_conclusion_maps_to_ready(self) -> None:
        pr = make_pr(number=1)
        ci = evaluate_ci([make_check_run(name="lint")])
        reviews_eval = evaluate_reviews([make_review()])
        legacy = evaluate_readiness(pr, ci, reviews_eval, PRTrackingState.CI_PASSING)
        # All requirements met -> success
        assert legacy.conclusion == "success"

        verdict = readiness_from_legacy(legacy)
        assert verdict.verdict == "ready"
        assert verdict.confidence == 1.0
        assert verdict.blockers == []
        assert verdict.summary

    def test_in_progress_conclusion_maps_to_pending(self) -> None:
        pr = make_pr(number=2)
        ci = evaluate_ci([make_check_run(name="test", status=CheckStatus.IN_PROGRESS)])
        reviews_eval = evaluate_reviews([])
        legacy = evaluate_readiness(pr, ci, reviews_eval, PRTrackingState.CI_PENDING)
        assert legacy.conclusion == "in_progress"
        verdict = readiness_from_legacy(legacy)
        assert verdict.verdict == "pending"

    def test_failure_conclusion_maps_to_blocked(self) -> None:
        # Changes-requested review + failing CI is the canonical hard-block
        # state: every component scores 0 and conclusion is ``failure``.
        pr = make_pr(number=3)
        ci = evaluate_ci([make_check_run(name="test", conclusion=CheckConclusion.FAILURE)])
        reviews_eval = evaluate_reviews(
            [make_review(state=ReviewState.CHANGES_REQUESTED, body="please fix")]
        )
        legacy = evaluate_readiness(pr, ci, reviews_eval, PRTrackingState.CI_FAILING)
        assert legacy.conclusion == "failure"
        verdict = readiness_from_legacy(legacy)
        assert verdict.verdict == "blocked"
        assert any(b.category == "ci_failing" for b in verdict.blockers)
        assert any(b.category == "review_outstanding" for b in verdict.blockers)

    @pytest.mark.parametrize(
        ("legacy_token", "expected_category"),
        [
            ("ci_failing", "ci_failing"),
            ("ci_pending", "ci_failing"),
            ("changes_requested", "review_outstanding"),
            ("automated_feedback_unaddressed", "review_outstanding"),
            ("required_review_missing", "approval_required"),
            ("merge_conflict", "merge_conflict"),
            ("draft_pr", "draft"),
            ("breaking_change", "policy_guard"),
            ("manual_hold", "policy_guard"),
            ("something_new_and_unknown", "other"),
        ],
    )
    def test_legacy_token_maps_to_category(self, legacy_token: str, expected_category: str) -> None:
        # Build a synthetic ReadinessEvaluation with just that token.
        from caretaker.pr_agent.states import ReadinessEvaluation

        legacy = ReadinessEvaluation(
            score=0.1,
            blockers=[legacy_token],
            summary=f"PR blocked: {legacy_token}",
            conclusion="failure",
        )
        verdict = readiness_from_legacy(legacy)
        assert len(verdict.blockers) == 1
        assert verdict.blockers[0].category == expected_category
        # Each translated blocker must carry human_reason + suggested_action.
        assert verdict.blockers[0].human_reason
        assert verdict.blockers[0].suggested_action

    def test_summary_truncated_to_schema_limit(self) -> None:
        from caretaker.pr_agent.states import ReadinessEvaluation

        legacy = ReadinessEvaluation(
            score=0.5,
            blockers=["ci_failing"],
            summary="x" * 500,
            conclusion="failure",
        )
        verdict = readiness_from_legacy(legacy)
        assert len(verdict.summary) <= 200


# ── Part D.3: LLM candidate prompt + happy path ──────────────────────────


class TestEvaluatePRReadinessLLM:
    def test_prompt_contains_required_payload(self) -> None:
        pr = make_pr(
            number=42,
            labels=[Label(name="maintainer:upgrade"), Label(name="area:config")],
        )
        pr = pr.model_copy(update={"title": "Migrate readiness gate", "body": "why not?"})
        ctx = PRReadinessContext(
            pr=pr,
            check_runs=[make_check_run(name="lint"), make_check_run(name="test")],
            reviews=[make_review(body="LGTM")],
            linked_issues=["#37"],
            repo_slug="ianlintner/caretaker",
            is_solo_maintainer=True,
        )
        prompt = build_readiness_prompt(ctx)
        assert "Migrate readiness gate" in prompt
        assert "#42" in prompt
        assert "ianlintner/caretaker" in prompt
        assert "maintainer:upgrade" in prompt
        assert "area:config" in prompt
        assert "Is solo maintainer repo: True" in prompt
        assert "lint" in prompt
        assert "LGTM" in prompt
        assert "#37" in prompt

    async def test_happy_path_returns_schema_instance(self) -> None:
        pr = make_pr(number=7)
        fake_verdict = Readiness(
            verdict="ready",
            confidence=0.92,
            blockers=[],
            summary="Solo repo, CI green, no review required.",
        )

        class _FakeClaude:
            def __init__(self) -> None:
                self.calls: list[dict[str, Any]] = []

            async def structured_complete(
                self,
                prompt: str,
                *,
                schema: type,
                feature: str,
                system: str | None = None,
            ) -> Any:
                self.calls.append(
                    {
                        "prompt": prompt,
                        "schema": schema,
                        "feature": feature,
                        "system": system,
                    }
                )
                return fake_verdict

        claude = _FakeClaude()
        ctx = PRReadinessContext(
            pr=pr,
            repo_slug="ian/demo",
            is_solo_maintainer=True,
        )
        result = await evaluate_pr_readiness_llm(ctx, claude=claude)  # type: ignore[arg-type]
        assert result is fake_verdict
        assert len(claude.calls) == 1
        call = claude.calls[0]
        assert call["feature"] == "pr_readiness"
        assert call["schema"] is Readiness
        # The variable-payload prompt contains the PR + repo metadata.
        assert "ian/demo" in call["prompt"]
        assert "#7" in call["prompt"]
        # System prefix is the stable prompt-cache prefix.
        assert call["system"] is not None
        assert "merge-readiness classifier" in call["system"]

    async def test_structured_complete_error_returns_none(self) -> None:
        pr = make_pr(number=9)

        claude = AsyncMock()
        claude.structured_complete.side_effect = StructuredCompleteError(
            raw_text="not-json", validation_error=ValueError("bad")
        )

        ctx = PRReadinessContext(pr=pr, repo_slug="ian/demo")
        result = await evaluate_pr_readiness_llm(ctx, claude=claude)
        assert result is None


# ── Part D.4: Status-comment rendering ───────────────────────────────────


class TestStatusCommentRendering:
    def _tracking(self) -> TrackedPR:
        return TrackedPR(number=1, readiness_score=1.0, readiness_blockers=[], readiness_summary="")

    def test_ready_verdict_uses_summary_verbatim(self) -> None:
        pr = make_pr(number=1)
        tracking = self._tracking()
        verdict = Readiness(
            verdict="ready",
            confidence=0.95,
            blockers=[],
            summary="CI green; solo maintainer gate satisfied.",
        )
        body = build_status_comment(pr, tracking, readiness_verdict=verdict)
        assert "✅ CI green; solo maintainer gate satisfied." in body
        assert "None — PR is ready!" in body

    def test_blocked_verdict_enumerates_blockers(self) -> None:
        pr = make_pr(number=2)
        tracking = self._tracking()
        verdict = Readiness(
            verdict="blocked",
            confidence=0.8,
            blockers=[
                {
                    "category": "ci_failing",
                    "human_reason": "The lint job failed on a trailing comma.",
                    "suggested_action": "Run ruff format and push.",
                },
                {
                    "category": "merge_conflict",
                    "human_reason": "Branch conflicts with main.",
                    "suggested_action": "Rebase onto main.",
                },
            ],
            summary="CI failing and branch out of date.",
        )
        body = build_status_comment(pr, tracking, readiness_verdict=verdict)
        assert "🚧 Blocked — CI failing and branch out of date." in body
        assert "**ci_failing**" in body
        assert "lint job failed" in body
        assert "Run ruff format and push." in body
        assert "**merge_conflict**" in body

    def test_needs_human_verdict_rendered(self) -> None:
        pr = make_pr(number=3)
        tracking = self._tracking()
        verdict = Readiness(
            verdict="needs_human",
            confidence=0.6,
            blockers=[
                {
                    "category": "waiting_for_upstream",
                    "human_reason": "Depends on upstream library fix.",
                    "suggested_action": "Escalate to maintainer.",
                }
            ],
            summary="Blocked by upstream fix outside our control.",
        )
        body = build_status_comment(pr, tracking, readiness_verdict=verdict)
        assert "🙋 Needs human —" in body
        assert "waiting_for_upstream" in body
        assert "Escalate to maintainer." in body

    def test_check_title_and_summary_use_verdict(self) -> None:
        tracking = self._tracking()
        verdict = Readiness(
            verdict="ready",
            confidence=0.9,
            blockers=[],
            summary="All green.",
        )
        title = get_readiness_check_title(tracking, verdict)
        assert title.startswith("Ready for merge")
        summary = get_readiness_check_summary(tracking, verdict)
        assert "All green." in summary


# ── Part D.5: Shadow decorator integration ───────────────────────────────


class TestShadowDecoratorIntegration:
    async def test_off_mode_skips_candidate(self) -> None:
        _set_readiness_mode("off")

        @shadow_decision("readiness")
        async def decide(*, legacy: Any, candidate: Any, context: Any = None) -> Readiness:
            raise AssertionError("wrapper short-circuits")

        legacy_called = 0
        candidate_called = 0

        async def legacy() -> Readiness:
            nonlocal legacy_called
            legacy_called += 1
            return Readiness(verdict="ready", confidence=1.0, summary="ok")

        async def candidate() -> Readiness | None:
            nonlocal candidate_called
            candidate_called += 1
            return Readiness(verdict="blocked", confidence=0.9, summary="different")

        verdict = await decide(legacy=legacy, candidate=candidate)
        assert verdict.verdict == "ready"
        assert legacy_called == 1
        assert candidate_called == 0
        assert recent_records() == []

    async def test_shadow_mode_records_disagreement(self) -> None:
        _set_readiness_mode("shadow")

        @shadow_decision("readiness")
        async def decide(*, legacy: Any, candidate: Any, context: Any = None) -> Readiness:
            raise AssertionError("wrapper short-circuits")

        async def legacy() -> Readiness:
            return Readiness(verdict="blocked", confidence=0.4, summary="legacy says blocked")

        async def candidate() -> Readiness | None:
            return Readiness(verdict="ready", confidence=0.9, summary="candidate says ready")

        verdict = await decide(
            legacy=legacy,
            candidate=candidate,
            context={"repo_slug": "ian/demo", "pr_number": 1},
        )
        # Shadow mode always returns legacy verdict.
        assert verdict.verdict == "blocked"
        records = recent_records()
        assert len(records) == 1
        assert records[0].outcome == "disagree"
        assert records[0].name == "readiness"
        assert records[0].repo_slug == "ian/demo"

    async def test_enforce_mode_promotes_candidate(self) -> None:
        _set_readiness_mode("enforce")

        @shadow_decision("readiness")
        async def decide(*, legacy: Any, candidate: Any, context: Any = None) -> Readiness:
            raise AssertionError("wrapper short-circuits")

        async def legacy() -> Readiness:
            return Readiness(verdict="blocked", confidence=0.4, summary="legacy")

        async def candidate() -> Readiness | None:
            return Readiness(verdict="ready", confidence=0.95, summary="candidate ready")

        verdict = await decide(legacy=legacy, candidate=candidate)
        assert verdict.verdict == "ready"
        # enforced_candidate outcome is counted but not persisted as a
        # disagreement record.
        records = recent_records()
        assert records == []

    async def test_enforce_mode_candidate_returns_none_falls_through(self) -> None:
        _set_readiness_mode("enforce")

        @shadow_decision("readiness")
        async def decide(*, legacy: Any, candidate: Any, context: Any = None) -> Readiness:
            raise AssertionError("wrapper short-circuits")

        async def legacy() -> Readiness:
            return Readiness(verdict="ready", confidence=1.0, summary="legacy ready")

        async def candidate() -> Readiness | None:
            # Simulating StructuredCompleteError inside the candidate.
            return None

        verdict = await decide(legacy=legacy, candidate=candidate)
        assert verdict.verdict == "ready"

    async def test_shadow_candidate_none_triggers_disagreement(self) -> None:
        """When the candidate returns ``None`` in shadow mode, the default ``==``
        compare would mark it a disagreement with the legacy verdict, but our
        :class:`Readiness` custom compare filters on ``verdict`` so a ``None``
        candidate collapses to ``candidate_error`` only when it raises. Here we
        assert the disagreement path still records the mismatch cleanly.
        """
        _set_readiness_mode("shadow")

        @shadow_decision("readiness")
        async def decide(*, legacy: Any, candidate: Any, context: Any = None) -> Readiness:
            raise AssertionError("wrapper short-circuits")

        async def legacy() -> Readiness:
            return Readiness(verdict="ready", confidence=1.0, summary="ok")

        async def candidate() -> Readiness | None:
            return None

        verdict = await decide(legacy=legacy, candidate=candidate)
        assert verdict.verdict == "ready"
        records = recent_records()
        assert len(records) == 1
        # Without a custom compare, ``Readiness(...) == None`` is False ->
        # disagreement. The record captures the mismatch for auditing.
        assert records[0].outcome == "disagree"
