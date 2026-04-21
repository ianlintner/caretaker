"""GitHub review posting helpers."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from caretaker.pr_reviewer.inline_reviewer import ReviewResult  # noqa: TC001 (runtime-used)

if TYPE_CHECKING:
    from caretaker.github_client.api import GitHubClient

logger = logging.getLogger(__name__)

_REVIEW_MARKER = "<!-- caretaker:pr-reviewer -->"

_VERDICT_TO_EVENT = {
    "APPROVE": "APPROVE",
    "REQUEST_CHANGES": "REQUEST_CHANGES",
    "COMMENT": "COMMENT",
}


async def post_review(
    *,
    github: GitHubClient,
    owner: str,
    repo: str,
    pr_number: int,
    commit_sha: str,
    result: ReviewResult,
    post_inline_comments: bool = True,
    force_event: str | None = None,
) -> None:
    """Submit a pull request review via the GitHub Reviews API."""
    event = force_event or _VERDICT_TO_EVENT.get(result.verdict, "COMMENT")

    body = f"{_REVIEW_MARKER}\n{result.summary}"

    inline_comments: list[dict[str, object]] = []
    if post_inline_comments and result.comments:
        for c in result.comments:
            if not c.path or not c.body:
                continue
            inline_comments.append(
                {
                    "path": c.path,
                    "line": c.line,
                    "body": c.body,
                    "side": "RIGHT",
                }
            )

    try:
        await github.create_review(
            owner=owner,
            repo=repo,
            pr_number=pr_number,
            commit_sha=commit_sha,
            body=body,
            event=event,
            comments=inline_comments if inline_comments else None,
        )
        logger.info(
            "pr-reviewer: posted %s review on %s/%s#%d (%d inline comments)",
            event,
            owner,
            repo,
            pr_number,
            len(inline_comments),
        )
    except Exception as exc:
        logger.warning(
            "pr-reviewer: failed to post review on %s/%s#%d: %s",
            owner,
            repo,
            pr_number,
            exc,
        )
        # Fallback: post as plain comment so the review is not lost
        fallback_body = (
            f"{_REVIEW_MARKER}\n**PR Review ({result.verdict})**\n\n{result.summary}"
        )
        if result.comments:
            fallback_body += "\n\n**Comments:**\n"
            for c in result.comments[:8]:
                fallback_body += f"\n- `{c.path}:{c.line}` — {c.body}"
        try:
            await github.upsert_issue_comment(
                owner, repo, pr_number, marker=_REVIEW_MARKER, body=fallback_body
            )
        except Exception as fallback_exc:
            logger.error(
                "pr-reviewer: fallback comment also failed for %s/%s#%d: %s",
                owner,
                repo,
                pr_number,
                fallback_exc,
            )
