"""Claude Code reviewer — slow path for complex PRs.

Instead of running a review inline, this applies the configured trigger label
and posts a structured ``@claude`` hand-off comment requesting a full code review.
The ``anthropics/claude-code-action`` workflow picks that up asynchronously.

This is intentionally thin: we delegate the hard work to the action so caretaker
doesn't need to manage execution context or tool-loop budget for large diffs.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from caretaker.config import PRReviewerConfig
    from caretaker.github_client.api import GitHubClient

logger = logging.getLogger(__name__)

_HANDOFF_MARKER = "<!-- caretaker:pr-reviewer-handoff -->"


def _build_handoff_comment(
    *,
    mention: str,
    pr_number: int,
    owner: str,
    repo: str,
    routing_reason: str,
) -> str:
    lines = [
        _HANDOFF_MARKER,
        f"{mention} caretaker is requesting a full code review for this PR.",
        "",
        f"**Repo:** `{owner}/{repo}` · **PR:** #{pr_number}",
        f"**Routing reason:** {routing_reason}",
        "",
        "Please review this pull request for:",
        "- Correctness and logic errors",
        "- Security vulnerabilities or unsafe patterns",
        "- API contract and backward-compatibility concerns",
        "- Test coverage gaps",
        "- Any blocking issues before merge",
        "",
        "Post a review comment summary and inline comments where applicable.",
        "",
        "_Delegated by caretaker's PRReviewerAgent via ClaudeCodeExecutor hand-off._",
    ]
    return "\n".join(lines)


async def dispatch(
    *,
    github: GitHubClient,
    owner: str,
    repo: str,
    pr_number: int,
    config: PRReviewerConfig,
    routing_reason: str,
) -> bool:
    """Apply trigger label + post hand-off comment. Returns True on success."""
    label = config.claude_code_label
    mention = config.claude_code_mention

    try:
        await github.ensure_label(
            owner, repo, label, color="7057ff", description="claude-code-action trigger"
        )
        await github.add_labels(owner, repo, pr_number, [label])
    except Exception as exc:
        logger.warning(
            "pr-reviewer: failed to apply trigger label %r to %s/%s#%d: %s",
            label,
            owner,
            repo,
            pr_number,
            exc,
        )
        return False

    comment_body = _build_handoff_comment(
        mention=mention,
        pr_number=pr_number,
        owner=owner,
        repo=repo,
        routing_reason=routing_reason,
    )
    try:
        await github.upsert_issue_comment(
            owner,
            repo,
            pr_number,
            marker=_HANDOFF_MARKER,
            body=comment_body,
        )
    except Exception as exc:
        logger.warning(
            "pr-reviewer: failed to post hand-off comment on %s/%s#%d: %s",
            owner,
            repo,
            pr_number,
            exc,
        )
        return False

    logger.info(
        "pr-reviewer: claude-code hand-off dispatched for %s/%s#%d (%s)",
        owner,
        repo,
        pr_number,
        routing_reason,
    )
    return True
