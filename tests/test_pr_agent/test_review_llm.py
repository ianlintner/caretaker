"""Tests for T-A4: LLM-backed review-comment classification.

Covers the new :class:`ReviewClassification` schema, the legacy →
schema adapter, the :func:`classify_review_comment_llm` candidate, and
the ``@shadow_decision``-wrapped :func:`analyze_reviews` dispatch.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from caretaker.config import AgenticConfig, AgenticDomainConfig
from caretaker.evolution import shadow_config
from caretaker.evolution.shadow import clear_records_for_tests, recent_records
from caretaker.github_client.models import ReviewState
from caretaker.llm.claude import StructuredCompleteError
from caretaker.pr_agent.review import (
    ReviewCommentType,
    analyze_reviews,
    classify_review_basic,
)
from caretaker.pr_agent.review_llm import (
    ReviewClassification,
    build_review_prompt,
    classify_review_comment_llm,
    classify_review_legacy_adapter,
    compare_classifications,
)
from tests.conftest import make_review


@pytest.fixture(autouse=True)
def _reset_shadow_state() -> None:
    """Clear ring buffer + active shadow config between tests."""
    clear_records_for_tests()
    shadow_config.reset_for_tests()


def _set_review_classification_mode(mode: str) -> None:
    cfg = AgenticConfig(review_classification=AgenticDomainConfig(mode=mode))  # type: ignore[arg-type]
    shadow_config.configure(cfg)


# ── Schema ──────────────────────────────────────────────────────────────


class TestReviewClassificationSchema:
    def test_happy_path_round_trip(self) -> None:
        verdict = ReviewClassification(
            kind="actionable",
            severity="blocker",
            summary_one_line="Security vuln: SQL injection in query builder",
            requires_code_change=True,
            suggested_prompt_to_copilot="Parameterise the query in build_sql().",
        )
        dumped = verdict.model_dump_json()
        restored = ReviewClassification.model_validate_json(dumped)
        assert restored == verdict

    def test_suggested_prompt_optional(self) -> None:
        verdict = ReviewClassification(
            kind="praise",
            severity="trivial",
            summary_one_line="Nice refactor",
            requires_code_change=False,
        )
        assert verdict.suggested_prompt_to_copilot is None

    def test_kind_literal_rejects_unknown(self) -> None:
        with pytest.raises(ValidationError):
            ReviewClassification(
                kind="not-a-real-kind",  # type: ignore[arg-type]
                severity="minor",
                summary_one_line="x",
                requires_code_change=False,
            )

    def test_severity_literal_rejects_unknown(self) -> None:
        with pytest.raises(ValidationError):
            ReviewClassification(
                kind="actionable",
                severity="catastrophic",  # type: ignore[arg-type]
                summary_one_line="x",
                requires_code_change=True,
            )

    def test_summary_length_capped(self) -> None:
        with pytest.raises(ValidationError):
            ReviewClassification(
                kind="discussion",
                severity="minor",
                summary_one_line="x" * 141,
                requires_code_change=False,
            )

    def test_suggested_prompt_length_capped(self) -> None:
        with pytest.raises(ValidationError):
            ReviewClassification(
                kind="actionable",
                severity="major",
                summary_one_line="x",
                requires_code_change=True,
                suggested_prompt_to_copilot="x" * 501,
            )


# ── Legacy adapter ──────────────────────────────────────────────────────


class TestLegacyAdapter:
    """Every current keyword → mapped ``kind`` / ``severity``."""

    def test_nit_maps_to_nitpick_trivial(self) -> None:
        review = make_review(
            state=ReviewState.CHANGES_REQUESTED,
            body="nit: rename this variable",
        )
        analysis = classify_review_basic(review)
        verdict = classify_review_legacy_adapter(analysis, review)
        assert verdict.kind == "nitpick"
        assert verdict.severity == "trivial"
        assert verdict.requires_code_change is False
        assert verdict.suggested_prompt_to_copilot is None

    def test_must_maps_to_actionable_blocker(self) -> None:
        review = make_review(
            state=ReviewState.CHANGES_REQUESTED,
            body="You must handle the timeout case before merge.",
        )
        analysis = classify_review_basic(review)
        verdict = classify_review_legacy_adapter(analysis, review)
        assert verdict.kind == "actionable"
        assert verdict.severity == "blocker"
        assert verdict.requires_code_change is True

    def test_blocker_keyword_maps_to_actionable_blocker(self) -> None:
        review = make_review(
            state=ReviewState.CHANGES_REQUESTED,
            body="This is a blocker — don't merge until the data race is fixed.",
        )
        analysis = classify_review_basic(review)
        verdict = classify_review_legacy_adapter(analysis, review)
        assert verdict.kind == "actionable"
        assert verdict.severity == "blocker"

    def test_bug_maps_to_actionable_major(self) -> None:
        review = make_review(
            state=ReviewState.CHANGES_REQUESTED,
            body="There's a bug here: off-by-one on the range end.",
        )
        analysis = classify_review_basic(review)
        verdict = classify_review_legacy_adapter(analysis, review)
        assert verdict.kind == "actionable"
        assert verdict.severity == "major"
        assert verdict.requires_code_change is True

    def test_trailing_question_maps_to_question_trivial(self) -> None:
        review = make_review(
            state=ReviewState.CHANGES_REQUESTED,
            body="Does this path handle the empty-batch case?",
        )
        analysis = classify_review_basic(review)
        verdict = classify_review_legacy_adapter(analysis, review)
        assert verdict.kind == "question"
        assert verdict.severity == "trivial"
        assert verdict.requires_code_change is False

    def test_plain_body_maps_to_discussion_minor(self) -> None:
        review = make_review(
            state=ReviewState.CHANGES_REQUESTED,
            body="I prefer the functional style here, but either way works.",
        )
        analysis = classify_review_basic(review)
        verdict = classify_review_legacy_adapter(analysis, review)
        assert verdict.kind == "discussion"
        assert verdict.severity == "minor"
        assert verdict.requires_code_change is False

    def test_approved_review_maps_to_praise_trivial(self) -> None:
        review = make_review(state=ReviewState.APPROVED, body="LGTM, nice catch")
        analysis = classify_review_basic(review)
        verdict = classify_review_legacy_adapter(analysis, review)
        assert verdict.kind == "praise"
        assert verdict.severity == "trivial"
        assert verdict.requires_code_change is False

    def test_summary_truncated_to_schema_limit(self) -> None:
        long_body = "x" * 300
        review = make_review(state=ReviewState.CHANGES_REQUESTED, body=long_body)
        analysis = classify_review_basic(review)
        verdict = classify_review_legacy_adapter(analysis, review)
        assert len(verdict.summary_one_line) <= 140

    def test_legacy_adapter_never_populates_suggested_prompt(self) -> None:
        """The legacy path has no synthesis step — always leaves the prompt null."""
        review = make_review(
            state=ReviewState.CHANGES_REQUESTED,
            body="must fix the null pointer deref in handler.py",
        )
        analysis = classify_review_basic(review)
        verdict = classify_review_legacy_adapter(analysis, review)
        assert verdict.suggested_prompt_to_copilot is None


# ── LLM candidate ───────────────────────────────────────────────────────


class TestClassifyReviewCommentLLM:
    def test_build_prompt_contains_required_payload(self) -> None:
        review = make_review(
            state=ReviewState.CHANGES_REQUESTED,
            body="Please parameterise the query.",
        )
        prompt = build_review_prompt(
            review,
            pr_title="Fix SQL injection",
            diff_hunk="- cur.execute(f'SELECT * FROM t WHERE id={id}')",
        )
        assert "Fix SQL injection" in prompt
        assert "Please parameterise the query." in prompt
        assert "Diff hunk the reviewer was looking at:" in prompt
        assert "cur.execute" in prompt
        # Reviewer login should always appear so the LLM has attribution.
        assert "@reviewer" in prompt

    def test_build_prompt_empty_body_safe(self) -> None:
        review = make_review(state=ReviewState.CHANGES_REQUESTED, body="")
        prompt = build_review_prompt(review)
        assert "(empty review body)" in prompt

    @pytest.mark.asyncio
    async def test_happy_path_returns_parsed_verdict(self) -> None:
        fake_verdict = ReviewClassification(
            kind="actionable",
            severity="blocker",
            summary_one_line="Security: parameterise the query.",
            requires_code_change=True,
            suggested_prompt_to_copilot="Replace f-string SQL with a parameterised query.",
        )
        claude = MagicMock()
        claude.available = True
        claude.structured_complete = AsyncMock(return_value=fake_verdict)

        review = make_review(
            state=ReviewState.CHANGES_REQUESTED,
            body="must fix SQLi",
        )
        result = await classify_review_comment_llm(
            review,
            claude=claude,
            pr_title="Harden query builder",
        )
        assert result is fake_verdict
        # Verify the structured_complete call signature matches the
        # Phase 2 convention.
        call_kwargs = claude.structured_complete.call_args.kwargs
        assert call_kwargs["schema"] is ReviewClassification
        assert call_kwargs["feature"] == "review_classification"
        assert "PR review-comment classifier" in call_kwargs["system"]

    @pytest.mark.asyncio
    async def test_structured_complete_error_returns_none(self) -> None:
        claude = MagicMock()
        claude.available = True
        claude.structured_complete = AsyncMock(
            side_effect=StructuredCompleteError(
                raw_text="not-json",
                validation_error=ValueError("bad"),
            )
        )

        review = make_review(state=ReviewState.CHANGES_REQUESTED, body="must fix")
        result = await classify_review_comment_llm(review, claude=claude)
        assert result is None

    @pytest.mark.asyncio
    async def test_unexpected_exception_returns_none(self) -> None:
        claude = MagicMock()
        claude.available = True
        claude.structured_complete = AsyncMock(side_effect=RuntimeError("boom"))

        review = make_review(state=ReviewState.CHANGES_REQUESTED, body="must fix")
        result = await classify_review_comment_llm(review, claude=claude)
        assert result is None


# ── Shadow-mode comparator ───────────────────────────────────────────────


class TestCompareClassifications:
    def _make(
        self,
        kind: str = "actionable",
        severity: str = "major",
        summary: str = "s",
        requires: bool = True,
    ) -> ReviewClassification:
        return ReviewClassification(
            kind=kind,  # type: ignore[arg-type]
            severity=severity,  # type: ignore[arg-type]
            summary_one_line=summary,
            requires_code_change=requires,
        )

    def test_same_kind_and_severity_agree(self) -> None:
        a = self._make(summary="one")
        b = self._make(summary="two")
        assert compare_classifications(a, b) is True

    def test_different_kind_disagree(self) -> None:
        a = self._make(kind="actionable", severity="major")
        b = self._make(kind="discussion", severity="major")
        assert compare_classifications(a, b) is False

    def test_different_severity_disagree(self) -> None:
        a = self._make(severity="blocker")
        b = self._make(severity="major")
        assert compare_classifications(a, b) is False


# ── Shadow decorator integration (3 modes) ───────────────────────────────


class TestAnalyzeReviewsShadow:
    @pytest.mark.asyncio
    async def test_off_mode_uses_legacy_only(self) -> None:
        _set_review_classification_mode("off")

        # No LLM router — exercise the default-safe path.
        reviews = [
            make_review(
                state=ReviewState.CHANGES_REQUESTED,
                body="must fix the null deref",
            )
        ]
        analyses = await analyze_reviews(reviews)
        assert len(analyses) == 1
        # Legacy heuristic on "must" currently drops into ACTIONABLE/moderate;
        # the new adapter upgrades the severity pass-through to blocker.
        assert analyses[0].comment_type == ReviewCommentType.ACTIONABLE
        assert analyses[0].severity == "blocker"
        # Off mode writes no shadow records.
        assert recent_records(name="review_classification") == []

    @pytest.mark.asyncio
    async def test_shadow_mode_records_disagreement_but_returns_legacy(self) -> None:
        _set_review_classification_mode("shadow")

        # LLM says nitpick/trivial; legacy keyword ladder will say
        # actionable/blocker (body contains "must").
        llm_verdict = ReviewClassification(
            kind="nitpick",
            severity="trivial",
            summary_one_line="Optional style suggestion",
            requires_code_change=False,
        )
        claude = MagicMock()
        claude.available = True
        claude.structured_complete = AsyncMock(return_value=llm_verdict)

        router = MagicMock()
        router.claude_available = True
        router.claude = claude

        reviews = [
            make_review(
                state=ReviewState.CHANGES_REQUESTED,
                body="you must rename this local variable to foo_bar",
            )
        ]
        analyses = await analyze_reviews(
            reviews,
            llm_router=router,
            pr_title="Rename locals",
            repo_slug="ian/demo",
        )
        # Shadow mode returns the legacy verdict unchanged.
        assert len(analyses) == 1
        assert analyses[0].severity == "blocker"

        records = recent_records(name="review_classification")
        assert len(records) == 1
        rec = records[0]
        assert rec.outcome == "disagree"
        assert rec.repo_slug == "ian/demo"

    @pytest.mark.asyncio
    async def test_shadow_mode_agrees_when_kind_and_severity_match(self) -> None:
        _set_review_classification_mode("shadow")

        llm_verdict = ReviewClassification(
            kind="actionable",
            severity="blocker",
            summary_one_line="Security must-fix, different wording from legacy",
            requires_code_change=True,
            suggested_prompt_to_copilot="Parameterise the query.",
        )
        claude = MagicMock()
        claude.available = True
        claude.structured_complete = AsyncMock(return_value=llm_verdict)

        router = MagicMock()
        router.claude_available = True
        router.claude = claude

        reviews = [
            make_review(
                state=ReviewState.CHANGES_REQUESTED,
                body="you must fix the SQL injection before merge",
            )
        ]
        await analyze_reviews(reviews, llm_router=router)

        records = recent_records(name="review_classification")
        assert len(records) == 1
        assert records[0].outcome == "agree"

    @pytest.mark.asyncio
    async def test_enforce_mode_promotes_candidate(self) -> None:
        _set_review_classification_mode("enforce")

        # Body is keyword-only ambiguous; legacy drops into discussion/minor.
        # LLM says blocker. Enforce should promote the LLM verdict.
        llm_verdict = ReviewClassification(
            kind="actionable",
            severity="blocker",
            summary_one_line="Unhandled error path crashes the worker",
            requires_code_change=True,
            suggested_prompt_to_copilot="Wrap the await in a try/except.",
        )
        claude = MagicMock()
        claude.available = True
        claude.structured_complete = AsyncMock(return_value=llm_verdict)

        router = MagicMock()
        router.claude_available = True
        router.claude = claude

        reviews = [
            make_review(
                state=ReviewState.CHANGES_REQUESTED,
                body="When the upstream returns 500 the worker dies silently.",
            )
        ]
        analyses = await analyze_reviews(reviews, llm_router=router)
        assert len(analyses) == 1
        assert analyses[0].severity == "blocker"
        assert analyses[0].comment_type == ReviewCommentType.ACTIONABLE
        assert analyses[0].classification is not None
        assert analyses[0].classification.suggested_prompt_to_copilot is not None

    @pytest.mark.asyncio
    async def test_enforce_falls_back_to_legacy_when_llm_unavailable(self) -> None:
        _set_review_classification_mode("enforce")

        reviews = [
            make_review(
                state=ReviewState.CHANGES_REQUESTED,
                body="nit: rename variable",
            )
        ]
        # No llm_router — candidate returns None, decorator falls through.
        analyses = await analyze_reviews(reviews, llm_router=None)
        assert len(analyses) == 1
        assert analyses[0].severity == "trivial"
        assert analyses[0].comment_type == ReviewCommentType.NITPICK


# ── Severity propagation downstream ──────────────────────────────────────


class TestSeverityPropagation:
    """Severity must survive through analyze_reviews → ReviewAnalysis.

    The Copilot bridge reads ``analysis.severity`` when building its
    task prompt + priority (see
    :func:`caretaker.pr_agent.copilot.PRCopilotBridge.request_review_fix`).
    These tests pin the field's shape and filtering behaviour so
    downstream consumers can rely on it.
    """

    @pytest.mark.asyncio
    async def test_blocker_severity_bubbles_to_analysis(self) -> None:
        _set_review_classification_mode("off")
        reviews = [
            make_review(
                state=ReviewState.CHANGES_REQUESTED,
                body="You must not merge this — data loss on cascade delete.",
            )
        ]
        analyses = await analyze_reviews(reviews)
        assert analyses[0].severity == "blocker"
        assert analyses[0].classification is not None
        assert analyses[0].classification.requires_code_change is True

    @pytest.mark.asyncio
    async def test_high_nitpick_threshold_drops_trivial_items(self) -> None:
        _set_review_classification_mode("off")
        reviews = [
            make_review(
                state=ReviewState.CHANGES_REQUESTED,
                body="nit: consider renaming",
            ),
            make_review(
                state=ReviewState.CHANGES_REQUESTED,
                body="bug: off-by-one on the range end",
            ),
        ]
        analyses = await analyze_reviews(reviews, nitpick_threshold="high")
        # The trivial nitpick is filtered; the major bug survives.
        assert len(analyses) == 1
        assert analyses[0].severity == "major"

    @pytest.mark.asyncio
    async def test_request_review_fix_prioritises_blocker(self) -> None:
        """The Copilot bridge should mark a blocker task as priority=high."""
        from caretaker.pr_agent.copilot import CopilotInteractionResult, PRCopilotBridge
        from caretaker.pr_agent.review import ReviewAnalysis
        from tests.conftest import make_pr

        captured: dict[str, Any] = {}

        bridge = PRCopilotBridge(
            protocol=MagicMock(),
            max_retries=2,
        )

        async def _fake_dispatch(
            *,
            pr: Any,
            copilot_task: Any,
            attempt: int,
            task_type_label: str,
        ) -> CopilotInteractionResult:
            captured["priority"] = copilot_task.priority
            return CopilotInteractionResult(
                task_posted=True,
                task_type=task_type_label,
                attempt=attempt,
                max_attempts=2,
                comment_id=1,
            )

        bridge._dispatch = _fake_dispatch  # type: ignore[method-assign]

        pr = make_pr(number=1)
        analyses = [
            ReviewAnalysis(
                reviewer="a",
                comment_type=ReviewCommentType.ACTIONABLE,
                summary="must-fix security bug",
                complexity="complex",
                body="You must fix the SQLi.",
                severity="blocker",
            ),
        ]
        await bridge.request_review_fix(pr=pr, analyses=analyses, attempt=1)
        assert captured["priority"] == "high"

    @pytest.mark.asyncio
    async def test_request_review_fix_default_priority_for_non_blockers(self) -> None:
        """Non-blocker severities stay at the pre-T-A4 ``medium`` priority."""
        from caretaker.pr_agent.copilot import CopilotInteractionResult, PRCopilotBridge
        from caretaker.pr_agent.review import ReviewAnalysis
        from tests.conftest import make_pr

        captured: dict[str, Any] = {}

        bridge = PRCopilotBridge(
            protocol=MagicMock(),
            max_retries=2,
        )

        async def _fake_dispatch(
            *,
            pr: Any,
            copilot_task: Any,
            attempt: int,
            task_type_label: str,
        ) -> CopilotInteractionResult:
            captured["priority"] = copilot_task.priority
            return CopilotInteractionResult(
                task_posted=True,
                task_type=task_type_label,
                attempt=attempt,
                max_attempts=2,
                comment_id=1,
            )

        bridge._dispatch = _fake_dispatch  # type: ignore[method-assign]

        pr = make_pr(number=1)
        analyses = [
            ReviewAnalysis(
                reviewer="a",
                comment_type=ReviewCommentType.ACTIONABLE,
                summary="minor nit",
                complexity="trivial",
                body="nit: rename",
                severity="trivial",
            ),
        ]
        await bridge.request_review_fix(pr=pr, analyses=analyses, attempt=1)
        assert captured["priority"] == "medium"
