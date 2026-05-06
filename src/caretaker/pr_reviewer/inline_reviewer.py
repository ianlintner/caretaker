"""Inline LLM reviewer — fast path for small/simple PRs.

Fetches the unified diff and asks the configured LLM for a structured review.
Returns a ``ReviewResult`` that ``github_review.post_review()`` can post directly.
"""

from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field

from caretaker.llm.claude import StructuredCompleteError
from caretaker.observability.tracer_compat import Status, StatusCode, get_tracer

if TYPE_CHECKING:
    from caretaker.github_client.api import GitHubClient
    from caretaker.llm.router import LLMRouter

logger = logging.getLogger(__name__)

# Module-level tracer — wraps each inline-LLM review so the parent
# ``pr_reviewer.handle_pr`` trace shows diff_lines / model / verdict.
_tracer = get_tracer("caretaker.pr_reviewer.inline_reviewer")

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

When verdict is REQUEST_CHANGES, also fill ``issue_categories`` with one
or more of: lint, format, type, test, security, correctness, docs, other.
This routes the auto-fix dispatcher: ``lint``/``format`` skip the LLM
and run a deterministic fixer; ``security``/``correctness`` get a heavy
agent. Order entries by impact — first one is the dominant category.
"""


class InlineReviewCommentModel(BaseModel):
    """Pydantic model for a single inline review comment."""

    path: str = Field(..., description="Path of the file being commented on.")
    line: int = Field(..., description="Line number in the new file (right side of diff).")
    body: str = Field(..., description="Review comment body, under 300 characters.")


# Auto-fix dispatch categories. Adding a new value here means caretaker
# can route a fixer-mode dispatch differently for that category. Keep
# the set small — the value of this field is letting the dispatcher pick
# a *cheaper* fixer for mechanical issues (lint), not exhaustively
# tagging every kind of feedback.
IssueCategory = Literal[
    "lint",
    "format",
    "type",
    "test",
    "security",
    "correctness",
    "docs",
    "other",
]


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
    issue_categories: list[IssueCategory] = Field(
        default_factory=list,
        description=(
            "When verdict is REQUEST_CHANGES, classify the issues so the "
            "auto-fix dispatcher can pick a cheap fixer (e.g. lint → "
            "deterministic ruff run, security → heavy LLM agent). Order "
            "by impact — first entry is the dominant category."
        ),
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
    # Optional issue classification used by the auto-fix dispatcher.
    # Empty list = unclassified; dispatcher falls back to the configured
    # default fixer. Order is preserved so callers can prefer the first
    # entry when picking exactly one fixer.
    issue_categories: list[str] = field(default_factory=list)


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
    with _tracer.start_as_current_span("inline_reviewer.review") as span:
        with contextlib.suppress(Exception):  # pragma: no cover - defensive
            span.set_attribute("caretaker.pr.repo", f"{owner}/{repo}")
            span.set_attribute("caretaker.pr.number", int(pr_number))

        diff = await github.get_pull_diff(owner, repo, pr_number)
        if not diff:
            with contextlib.suppress(Exception):  # pragma: no cover
                span.set_attribute("caretaker.review.diff_lines", 0)
                span.set_attribute("caretaker.review.verdict", "COMMENT")
            return ReviewResult(
                summary="Could not fetch diff — skipping inline review.",
                verdict="COMMENT",
            )

        diff_lines = diff.splitlines()
        bounded_lines = min(len(diff_lines), max_diff_lines)
        if len(diff_lines) > max_diff_lines:
            diff = "\n".join(diff_lines[:max_diff_lines]) + "\n…(diff truncated)"

        with contextlib.suppress(Exception):  # pragma: no cover
            span.set_attribute("caretaker.review.diff_lines", int(bounded_lines))

        prompt = (
            f"PR #{pr_number}: {pr_title}\n\n"
            f"{pr_body.strip()[:500] if pr_body else '(no description)'}\n\n"
            "---\n"
            f"```diff\n{diff}\n```"
        )

        # Capture the model the LLM router reports for this feature, when
        # accessible — best-effort because the router shape varies.
        with contextlib.suppress(Exception):  # pragma: no cover
            model_name = getattr(getattr(llm, "claude", None), "model", "") or ""
            if model_name:
                span.set_attribute("caretaker.review.model", str(model_name))

        try:
            payload = await llm.claude.structured_complete(
                prompt,
                schema=InlineReviewResult,
                feature="pr_inline_review",
                system=_REVIEW_SYSTEM,
                max_tokens=2000,
            )
        except StructuredCompleteError as exc:
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
            with contextlib.suppress(Exception):  # pragma: no cover
                span.record_exception(exc)
                span.set_status(Status(StatusCode.ERROR, str(exc)[:200]))
            raise
        except Exception as exc:
            logger.warning("Inline LLM review failed for %s/%s#%d: %s", owner, repo, pr_number, exc)
            with contextlib.suppress(Exception):  # pragma: no cover
                span.set_attribute("caretaker.review.verdict", "COMMENT")
            return ReviewResult(
                summary=f"Inline review failed: {exc}",
                verdict="COMMENT",
            )

        comments = [
            InlineReviewComment(path=c.path, line=int(c.line), body=c.body)
            for c in payload.comments
            if c.path and c.body
        ]

        with contextlib.suppress(Exception):  # pragma: no cover
            span.set_attribute("caretaker.review.verdict", str(payload.verdict))

        return ReviewResult(
            summary=payload.summary,
            verdict=payload.verdict,
            comments=comments,
            raw_response=payload.model_dump_json(),
            issue_categories=list(payload.issue_categories),
        )
