"""Tests for review comment analysis."""

from __future__ import annotations

import pytest

from caretaker.github_client.models import ReviewState, User
from caretaker.pr_agent.review import (
    ReviewCommentType,
    classify_review_basic,
    analyze_reviews,
)

from tests.conftest import make_review


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
