"""Review comment handling for PRs."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from caretaker.github_client.models import Review, ReviewState
from caretaker.pr_agent._constants import is_automated_reviewer

if TYPE_CHECKING:
    from caretaker.llm.router import LLMRouter

logger = logging.getLogger(__name__)


class ReviewCommentType(StrEnum):
    ACTIONABLE = "ACTIONABLE"
    NITPICK = "NITPICK"
    QUESTION = "QUESTION"
    PRAISE = "PRAISE"
    UNKNOWN = "UNKNOWN"


@dataclass
class ReviewAnalysis:
    reviewer: str
    comment_type: ReviewCommentType
    summary: str
    complexity: str  # trivial, moderate, complex
    body: str


def classify_review_basic(review: Review) -> ReviewAnalysis:
    """Basic review classification using heuristics."""
    body_lower = review.body.lower()

    if review.state == ReviewState.APPROVED:
        return ReviewAnalysis(
            reviewer=review.user.login,
            comment_type=ReviewCommentType.PRAISE,
            summary="Approval",
            complexity="trivial",
            body=review.body,
        )

    # Simple keyword matching for classification
    if any(w in body_lower for w in ["nit:", "nitpick", "optional", "consider", "minor"]):
        comment_type = ReviewCommentType.NITPICK
        complexity = "trivial"
    elif any(w in body_lower for w in ["?", "why", "what", "how", "could you explain"]):
        comment_type = ReviewCommentType.QUESTION
        complexity = "trivial"
    elif any(
        w in body_lower
        for w in [
            "bug",
            "error",
            "wrong",
            "fix",
            "must",
            "should",
            "required",
            "missing",
            "incorrect",
            "add test",
            "security",
        ]
    ):
        comment_type = ReviewCommentType.ACTIONABLE
        complexity = "moderate"
    else:
        comment_type = ReviewCommentType.ACTIONABLE
        complexity = "moderate"

    summary = review.body[:200] if review.body else "No comment body"

    return ReviewAnalysis(
        reviewer=review.user.login,
        comment_type=comment_type,
        summary=summary,
        complexity=complexity,
        body=review.body,
    )


async def analyze_reviews(
    reviews: list[Review],
    nitpick_threshold: str = "low",
    llm_router: LLMRouter | None = None,
) -> list[ReviewAnalysis]:
    """Analyze all blocking and automated-bot review comments."""
    analyses: list[ReviewAnalysis] = []

    # Formal CHANGES_REQUESTED reviews always count as blocking.
    # COMMENTED reviews from automated reviewer bots also carry actionable feedback.
    actionable = [
        r
        for r in reviews
        if r.state == ReviewState.CHANGES_REQUESTED
        or (r.state == ReviewState.COMMENTED and r.body and is_automated_reviewer(r.user.login))
    ]

    for review in actionable:
        if llm_router and llm_router.feature_enabled("architectural_review"):
            try:
                result = await llm_router.claude.analyze_review_comment(review.body, "")
                # Parse Claude's response
                analysis = ReviewAnalysis(
                    reviewer=review.user.login,
                    comment_type=ReviewCommentType.ACTIONABLE,
                    summary=result[:200],
                    complexity="moderate",
                    body=review.body,
                )
                analyses.append(analysis)
                continue
            except Exception:
                logger.warning("Claude review analysis failed, falling back")

        analysis = classify_review_basic(review)
        analyses.append(analysis)

    # Filter by nitpick threshold
    if nitpick_threshold == "high":
        analyses = [a for a in analyses if a.comment_type != ReviewCommentType.NITPICK]

    return analyses
