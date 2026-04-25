"""Tests for review comment analysis."""

from __future__ import annotations

import pytest

from caretaker.github_client.models import ReviewState
from caretaker.pr_agent.review import (
    ReviewAnalysis,
    ReviewCommentType,
    ReviewVerdict,
    analyze_reviews,
    assess_review_verdict,
    classify_review_basic,
)
from tests.conftest import make_review


def _analysis(body: str, severity: str = "minor") -> ReviewAnalysis:
    return ReviewAnalysis(
        reviewer="bot",
        comment_type=ReviewCommentType.ACTIONABLE,
        summary=body[:200],
        complexity="moderate",
        body=body,
        severity=severity,  # type: ignore[arg-type]
    )


class TestClassifyReviewBasic:
    def test_approval(self) -> None:
        review = make_review(state=ReviewState.APPROVED)
        result = classify_review_basic(review)
        assert result.comment_type == ReviewCommentType.PRAISE
        assert result.complexity == "trivial"

    def test_nitpick(self) -> None:
        review = make_review(
            state=ReviewState.CHANGES_REQUESTED,
            body="nit: rename this variable",
        )
        result = classify_review_basic(review)
        assert result.comment_type == ReviewCommentType.NITPICK
        assert result.complexity == "trivial"

    def test_optional_is_nitpick(self) -> None:
        review = make_review(
            state=ReviewState.CHANGES_REQUESTED,
            body="optional: consider using a list comprehension",
        )
        result = classify_review_basic(review)
        assert result.comment_type == ReviewCommentType.NITPICK

    def test_question(self) -> None:
        review = make_review(
            state=ReviewState.CHANGES_REQUESTED,
            body="Why did you choose this approach?",
        )
        result = classify_review_basic(review)
        assert result.comment_type == ReviewCommentType.QUESTION

    def test_actionable_bug(self) -> None:
        review = make_review(
            state=ReviewState.CHANGES_REQUESTED,
            body="This is a bug — the error handling is wrong",
        )
        result = classify_review_basic(review)
        assert result.comment_type == ReviewCommentType.ACTIONABLE

    def test_actionable_security(self) -> None:
        review = make_review(
            state=ReviewState.CHANGES_REQUESTED,
            body="This has a security vulnerability, must fix",
        )
        result = classify_review_basic(review)
        assert result.comment_type == ReviewCommentType.ACTIONABLE

    def test_generic_changes_requested(self) -> None:
        review = make_review(
            state=ReviewState.CHANGES_REQUESTED,
            body="Please refactor this function",
        )
        result = classify_review_basic(review)
        assert result.comment_type == ReviewCommentType.ACTIONABLE


class TestAnalyzeReviews:
    @pytest.mark.asyncio
    async def test_empty_reviews(self) -> None:
        result = await analyze_reviews([])
        assert result == []

    @pytest.mark.asyncio
    async def test_only_approvals_ignored(self) -> None:
        """analyze_reviews only processes blocking reviews."""
        reviews = [make_review(state=ReviewState.APPROVED)]
        result = await analyze_reviews(reviews)
        assert result == []

    @pytest.mark.asyncio
    async def test_changes_requested_analyzed(self) -> None:
        reviews = [
            make_review(state=ReviewState.CHANGES_REQUESTED, body="Fix this bug"),
        ]
        result = await analyze_reviews(reviews)
        assert len(result) == 1
        assert result[0].comment_type == ReviewCommentType.ACTIONABLE

    @pytest.mark.asyncio
    async def test_high_nitpick_threshold_filters(self) -> None:
        """High threshold filters out nitpick comments."""
        reviews = [
            make_review(
                state=ReviewState.CHANGES_REQUESTED,
                body="nit: rename variable",
            ),
        ]
        result = await analyze_reviews(reviews, nitpick_threshold="high")
        assert result == []

    @pytest.mark.asyncio
    async def test_low_nitpick_threshold_keeps(self) -> None:
        reviews = [
            make_review(
                state=ReviewState.CHANGES_REQUESTED,
                body="nit: rename variable",
            ),
        ]
        result = await analyze_reviews(reviews, nitpick_threshold="low")
        assert len(result) == 1


# ── assess_review_verdict ────────────────────────────────────────────


class TestAssessReviewVerdict:
    def test_empty_analyses_returns_approve(self) -> None:
        verdict, reason = assess_review_verdict([])
        assert verdict == ReviewVerdict.APPROVE
        assert "No blocking" in reason

    def test_normal_fixable_comment_returns_fix(self) -> None:
        analyses = [_analysis("This function is missing error handling")]
        verdict, _ = assess_review_verdict(analyses)
        assert verdict == ReviewVerdict.FIX

    def test_duplicate_signal_returns_close(self) -> None:
        analyses = [_analysis("This is a duplicate of the existing implementation")]
        verdict, reason = assess_review_verdict(analyses)
        assert verdict == ReviewVerdict.CLOSE
        assert "duplicate" in reason.lower() or "Infeasible" in reason

    def test_wont_work_signal_returns_close(self) -> None:
        analyses = [_analysis("This approach won't work because of the async event loop")]
        verdict, _ = assess_review_verdict(analyses)
        assert verdict == ReviewVerdict.CLOSE

    def test_infeasible_signal_returns_close(self) -> None:
        analyses = [_analysis("The approach is not feasible given the current architecture")]
        verdict, _ = assess_review_verdict(analyses)
        assert verdict == ReviewVerdict.CLOSE

    def test_out_of_scope_returns_close(self) -> None:
        analyses = [_analysis("This change is out of scope for this sprint")]
        verdict, _ = assess_review_verdict(analyses)
        assert verdict == ReviewVerdict.CLOSE

    def test_architectural_change_returns_escalate(self) -> None:
        analyses = [_analysis("This requires an architectural change to the data layer")]
        verdict, reason = assess_review_verdict(analyses)
        assert verdict == ReviewVerdict.ESCALATE
        assert "Architectural" in reason

    def test_major_refactor_returns_escalate(self) -> None:
        analyses = [_analysis("This needs a significant refactor of the storage layer")]
        verdict, _ = assess_review_verdict(analyses)
        assert verdict == ReviewVerdict.ESCALATE

    def test_blocker_high_loc_returns_escalate(self) -> None:
        """Blocker severity on a high-LoC PR escalates instead of fixes."""
        analyses = [_analysis("Missing auth check — must fix", severity="blocker")]
        verdict, reason = assess_review_verdict(analyses, pr_additions=600, high_loc_threshold=500)
        assert verdict == ReviewVerdict.ESCALATE
        assert "600" in reason

    def test_blocker_low_loc_returns_fix(self) -> None:
        """Blocker severity on a small PR is still routed to FIX."""
        analyses = [_analysis("Missing auth check — must fix", severity="blocker")]
        verdict, _ = assess_review_verdict(analyses, pr_additions=50, high_loc_threshold=500)
        assert verdict == ReviewVerdict.FIX

    def test_close_signal_beats_escalate_signal(self) -> None:
        """First matching analysis wins; close signal earlier in list wins."""
        analyses = [
            _analysis("This is a duplicate, not feasible"),
            _analysis("Needs significant refactor"),
        ]
        verdict, _ = assess_review_verdict(analyses)
        assert verdict == ReviewVerdict.CLOSE
