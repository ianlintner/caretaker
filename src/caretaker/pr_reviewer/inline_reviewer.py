"""Inline LLM reviewer — fast path for small/simple PRs.

Fetches the unified diff and asks the configured LLM for a structured review.
Returns a ``ReviewResult`` that ``github_review.post_review()`` can post directly.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field

from caretaker.llm.claude import StructuredCompleteError

if TYPE_CHECKING:
    from caretaker.github_client.api import GitHubClient
    from caretaker.llm.router import LLMRouter

logger = logging.getLogger(__name__)

_REVIEW_SYSTEM = """\
You are an expert code reviewer. Review the pull request diff below and produce
a concise, actionable review. Focus on correctness, security, and maintainability.

Rules:
- verdict = APPROVE   when the diff looks correct and ready to merge
- verdict = COMMENT   when you have observations but no blockers
- verdict = REQUEST_CHANGES when there are correctness/security issues
- comments must reference the new file line (right side of the diff)
- limit comments to at most 8 items; omit trivial nits
- keep each comment body under 300 characters
"""


class InlineReviewCommentModel(BaseModel):
    """Pydantic model for a single inline review comment."""

    path: str = Field(..., description="Path of the file being commented on.")
    line: int = Field(..., description="Line number in the new file (right side of diff).")
    body: str = Field(..., description="Review comment body, under 300 characters.")


class InlineReviewResult(BaseModel):
    """Structured LLM review payload — validated schema for ``structured_complete``."""

    summary: str = Field(..., description="1-3 sentence overall assessment.")
    verdict: Literal["APPROVE", "COMMENT", "REQUEST_CHANGES"] = Field(
        ..., description="Review verdict."
    )
    comments: list[InlineReviewCommentModel] = Field(
        default_factory=list,
        description="At most 8 line-scoped comments.",
    )


@dataclass
class InlineReviewComment:
    path: str
    line: int
    body: str


@dataclass
class ReviewResult:
    summary: str
    verdict: str  # APPROVE | COMMENT | REQUEST_CHANGES
    comments: list[InlineReviewComment] = field(default_factory=list)
    raw_response: str = ""


async def review(
    *,
    github: GitHubClient,
    owner: str,
    repo: str,
    pr_number: int,
    pr_title: str,
    pr_body: str,
    llm: LLMRouter,
    max_diff_lines: int = 2000,
) -> ReviewResult:
    """Fetch the PR diff and call the LLM for a review."""
    diff = await github.get_pull_diff(owner, repo, pr_number)
    if not diff:
        return ReviewResult(
            summary="Could not fetch diff — skipping inline review.",
            verdict="COMMENT",
        )

    diff_lines = diff.splitlines()
    if len(diff_lines) > max_diff_lines:
        diff = "\n".join(diff_lines[:max_diff_lines]) + "\n…(diff truncated)"

    prompt = (
        f"PR #{pr_number}: {pr_title}\n\n"
        f"{pr_body.strip()[:500] if pr_body else '(no description)'}\n\n"
        "---\n"
        f"```diff\n{diff}\n```"
    )

    try:
        payload = await llm.claude.structured_complete(
            prompt,
            schema=InlineReviewResult,
            feature="pr_inline_review",
            system=_REVIEW_SYSTEM,
            max_tokens=2000,
        )
    except StructuredCompleteError:
        # Surface parse/validation failures to the caller so they can be logged
        # loudly. The pr-reviewer agent is expected to catch and fall back to
        # a skip / claude-code dispatch — it must not silently issue an empty
        # COMMENT review as the old ``json.loads`` fallback did.
        logger.exception(
            "inline review for %s/%s#%d failed validation after retries",
            owner,
            repo,
            pr_number,
        )
        raise
    except Exception as exc:
        logger.warning("Inline LLM review failed for %s/%s#%d: %s", owner, repo, pr_number, exc)
        return ReviewResult(
            summary=f"Inline review failed: {exc}",
            verdict="COMMENT",
        )

    comments = [
        InlineReviewComment(path=c.path, line=int(c.line), body=c.body)
        for c in payload.comments
        if c.path and c.body
    ]

    return ReviewResult(
        summary=payload.summary,
        verdict=payload.verdict,
        comments=comments,
        raw_response=payload.model_dump_json(),
    )
