"""Inline LLM reviewer — fast path for small/simple PRs.

Fetches the unified diff and asks the configured LLM for a structured review.
Returns a ``ReviewResult`` that ``github_review.post_review()`` can post directly.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from caretaker.github_client.api import GitHubClient
    from caretaker.llm.router import LLMRouter

logger = logging.getLogger(__name__)

_REVIEW_SYSTEM = """\
You are an expert code reviewer. Review the pull request diff below and produce
a concise, actionable review. Focus on correctness, security, and maintainability.
Respond ONLY with a JSON object matching the schema described in the user message.
Do NOT wrap with markdown code fences. Output raw JSON only.
"""

_REVIEW_SCHEMA = """\
{
  "summary": "<1-3 sentence overall assessment>",
  "verdict": "APPROVE" | "COMMENT" | "REQUEST_CHANGES",
  "comments": [
    {
      "path": "<file path>",
      "line": <line number, integer>,
      "body": "<review comment text>"
    }
  ]
}

Rules:
- verdict = APPROVE   when the diff looks correct and ready to merge
- verdict = COMMENT   when you have observations but no blockers
- verdict = REQUEST_CHANGES when there are correctness/security issues
- comments must reference the new file line (right side of the diff)
- limit comments to at most 8 items; omit trivial nits
- keep each comment body under 300 characters
"""


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
        f"```diff\n{diff}\n```\n\n"
        "Respond with a JSON object matching this schema:\n"
        f"{_REVIEW_SCHEMA}"
    )

    try:
        raw = await llm.claude.complete(
            feature="pr_inline_review",
            system=_REVIEW_SYSTEM,
            prompt=prompt,
            max_tokens=2000,
        )
    except Exception as exc:
        logger.warning("Inline LLM review failed for %s/%s#%d: %s", owner, repo, pr_number, exc)
        return ReviewResult(
            summary=f"Inline review failed: {exc}",
            verdict="COMMENT",
        )

    raw_text = raw.strip()
    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError:
        logger.warning("LLM response not valid JSON for %s/%s#%d", owner, repo, pr_number)
        return ReviewResult(summary=raw_text[:500], verdict="COMMENT", raw_response=raw_text)

    comments = [
        InlineReviewComment(
            path=c.get("path", ""),
            line=int(c.get("line", 1)),
            body=c.get("body", ""),
        )
        for c in data.get("comments", [])
        if c.get("path") and c.get("body")
    ]

    return ReviewResult(
        summary=data.get("summary", ""),
        verdict=data.get("verdict", "COMMENT").upper(),
        comments=comments,
        raw_response=raw_text,
    )
