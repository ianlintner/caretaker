"""Harvest structured review payloads from BYOCA hand-off replies.

When caretaker delegates a complex review via
:mod:`caretaker.pr_reviewer.handoff_reviewer`, the hand-off comment asks
the upstream agent (Claude Code, opencode, …) to terminate its reply
with the marker :data:`REVIEW_RESULT_MARKER` followed by a
``caretaker-review`` fenced JSON block. This consumer scans the PR's
issue-comment thread on every cycle, finds unconsumed agent replies
that carry the marker, parses the JSON, and re-posts the content as a
*formal* GitHub PR review via :func:`post_review` so it shows up in the
**Reviews** tab (not just the comment thread).

The formal review is attributed to ``the-care-taker[bot]`` — caretaker
is the one calling the Reviews API. The body cites the originating
agent so reviewers can see the chain of custody.

Idempotency: each consumed comment ID is recorded in
``TrackedPR.consumed_handoff_review_comment_ids`` so re-runs don't
re-post the same review.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from caretaker.pr_reviewer.github_review import post_review
from caretaker.pr_reviewer.handoff_reviewer import (
    CLAUDE_CODE_REVIEW_MARKER,
    OPENCODE_REVIEW_MARKER,
)
from caretaker.pr_reviewer.inline_reviewer import InlineReviewComment, ReviewResult

# Per-backend hand-off invitation markers — caretaker writes these on
# the comment that *asks* the agent for a review. They must never be
# treated as the agent's response, even when the invitation body
# quotes :data:`REVIEW_RESULT_MARKER` for documentation purposes (it
# does today — the invitation includes a worked example so the agent
# knows what JSON shape to emit).
_HANDOFF_INVITATION_MARKERS = frozenset({CLAUDE_CODE_REVIEW_MARKER, OPENCODE_REVIEW_MARKER})

if TYPE_CHECKING:
    from caretaker.github_client.api import GitHubClient
    from caretaker.github_client.models import Comment
    from caretaker.state.models import TrackedPR

logger = logging.getLogger(__name__)


# A fenced ```caretaker-review … ``` block. Tolerant of leading/trailing
# whitespace and a CRLF mix that some upstream actions emit. The content
# group is non-greedy so a comment with multiple fences picks the first
# tagged one — agents only emit one per reply.
_PAYLOAD_RE = re.compile(
    r"```caretaker-review\s*\n(?P<json>.+?)\n\s*```",
    re.DOTALL,
)


@dataclass(frozen=True)
class _ParsedReview:
    """Internal — the parsed shape of an agent's review payload."""

    summary: str
    verdict: str  # APPROVE | COMMENT | REQUEST_CHANGES
    comments: list[InlineReviewComment]


def parse_review_payload(comment_body: str) -> _ParsedReview | None:
    """Extract a ``caretaker-review`` JSON block from a comment body.

    Returns ``None`` when no JSON block is present, the JSON is
    malformed, or the schema doesn't validate. The caller should treat
    ``None`` as "no formal review to post; the agent's plain comment
    stays as-is" rather than as an error condition.

    The ``caretaker:review-result`` HTML comment marker is *optional*:
    in practice agents (Claude Code observed in the v0.24.0 live QA
    cycle) emit the JSON fence correctly but drop the HTML comment
    during their output formatting. The fenced ```` ```caretaker-review
    ```` tag is unique enough to serve as the primary signal on its own.
    The HTML marker remains documented as a hint for agents that
    preserve it (and as a useful grep target).
    """
    match = _PAYLOAD_RE.search(comment_body)
    if not match:
        return None
    raw = match.group("json").strip()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("handoff_review: malformed JSON in caretaker-review block: %s", exc)
        return None
    if not isinstance(payload, dict):
        logger.warning("handoff_review: caretaker-review payload is not a JSON object")
        return None
    summary = payload.get("summary")
    verdict = payload.get("verdict", "COMMENT")
    if not isinstance(summary, str) or not summary.strip():
        logger.warning("handoff_review: payload missing required ``summary`` string")
        return None
    if verdict not in {"APPROVE", "COMMENT", "REQUEST_CHANGES"}:
        logger.warning("handoff_review: invalid verdict %r; defaulting to COMMENT", verdict)
        verdict = "COMMENT"
    raw_comments = payload.get("comments", []) or []
    if not isinstance(raw_comments, list):
        logger.warning("handoff_review: ``comments`` must be a list; ignoring")
        raw_comments = []
    parsed_comments: list[InlineReviewComment] = []
    for entry in raw_comments[:8]:  # cap matches inline_reviewer schema
        if not isinstance(entry, dict):
            continue
        path = entry.get("path")
        line = entry.get("line")
        body = entry.get("body")
        if (
            not isinstance(path, str)
            or not path
            or not isinstance(line, int)
            or line <= 0
            or not isinstance(body, str)
            or not body.strip()
        ):
            continue
        parsed_comments.append(InlineReviewComment(path=path, line=line, body=body.strip()))
    return _ParsedReview(summary=summary.strip(), verdict=verdict, comments=parsed_comments)


def _is_caretaker_authored(comment: Comment) -> bool:
    """Return True if the comment is one caretaker itself wrote.

    Detection is by *invitation marker* — every caretaker-authored
    hand-off comment carries one of the per-backend handoff markers
    (:data:`CLAUDE_CODE_REVIEW_MARKER` or :data:`OPENCODE_REVIEW_MARKER`).

    The previous implementation also tried to use "absence of the
    response marker" as exonerating evidence — but the v0.24.0
    invitation deliberately quotes the response marker for
    documentation purposes (it shows the agent what shape to emit), so
    that predicate now misclassifies the invitation as an agent reply.
    Doing nothing else has been protecting us in production only
    because the documentation example contains JSON-with-comments
    (intentionally invalid JSON), so ``parse_review_payload`` returns
    ``None`` and the consumer skips with a warning. A future copy edit
    that produced strictly-valid example JSON would post a fake formal
    review with placeholder content. Detecting the invitation marker
    directly closes that gap.
    """
    body = comment.body or ""
    return any(marker in body for marker in _HANDOFF_INVITATION_MARKERS)


async def consume_handoff_reviews(
    *,
    github: GitHubClient,
    owner: str,
    repo: str,
    pr_number: int,
    head_sha: str,
    tracking: TrackedPR,
) -> int:
    """Scan the PR for unconsumed agent review replies and post them.

    Returns the number of formal reviews posted. Idempotent: a comment
    whose ID is already in ``tracking.consumed_handoff_review_comment_ids``
    is skipped, and the ID is recorded only after a successful
    ``post_review`` call so a transient API failure on one cycle is
    automatically retried on the next.
    """
    if not head_sha:
        # Without a commit SHA we can't anchor inline comments — skip
        # rather than post a review against the wrong base.
        logger.debug("handoff_review: PR #%d has no head_sha; skipping", pr_number)
        return 0
    try:
        comments = await github.get_pr_comments(owner, repo, pr_number)
    except Exception as exc:  # noqa: BLE001 — never fail the agent
        logger.warning(
            "handoff_review: failed to list comments on %s/%s#%d: %s",
            owner,
            repo,
            pr_number,
            exc,
        )
        return 0

    consumed_ids = set(tracking.consumed_handoff_review_comment_ids)
    posted = 0
    for comment in comments:
        if comment.id in consumed_ids:
            continue
        body = comment.body or ""
        # Cheap pre-filter — must contain the fenced JSON tag to be a
        # candidate. Skips the vast majority of unrelated PR comments
        # without paying for the regex compile / parse.
        if "```caretaker-review" not in body:
            continue
        if _is_caretaker_authored(comment):
            # Caretaker's own hand-off invitation embeds the
            # ``caretaker-review`` fence as a *documentation example*
            # so the agent knows what shape to emit. Detected by the
            # handoff-specific marker (see ``_is_caretaker_authored``)
            # rather than the response marker — agents have been
            # observed dropping the response HTML comment while
            # preserving the fence (Claude Code in v0.24.0 live QA),
            # so the response marker is no longer a reliable
            # exonerating signal.
            continue
        parsed = parse_review_payload(body)
        if parsed is None:
            # Fence present but payload unparseable — record the ID so
            # we don't re-scan the same broken comment forever, but log
            # at warning so an operator can investigate.
            logger.warning(
                "handoff_review: comment %d on %s/%s#%d has caretaker-review "
                "fence but invalid payload; skipping permanently",
                comment.id,
                owner,
                repo,
                pr_number,
            )
            tracking.consumed_handoff_review_comment_ids.append(comment.id)
            continue

        author = comment.user.login or "unknown-agent"
        attribution = (
            f"_Formal review posted by caretaker on behalf of `@{author}` — "
            f"see [original reply](#issuecomment-{comment.id}) for full context._\n\n"
            f"{parsed.summary}"
        )
        try:
            await post_review(
                github=github,
                owner=owner,
                repo=repo,
                pr_number=pr_number,
                commit_sha=head_sha,
                result=ReviewResult(
                    summary=attribution,
                    verdict=parsed.verdict,
                    comments=parsed.comments,
                ),
            )
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.warning(
                "handoff_review: failed to post review for comment %d on "
                "%s/%s#%d: %s — will retry next cycle",
                comment.id,
                owner,
                repo,
                pr_number,
                exc,
            )
            continue

        tracking.consumed_handoff_review_comment_ids.append(comment.id)
        posted += 1
        logger.info(
            "handoff_review: posted formal review for comment %d (%s) on %s/%s#%d "
            "(%d inline comments, verdict=%s)",
            comment.id,
            author,
            owner,
            repo,
            pr_number,
            len(parsed.comments),
            parsed.verdict,
        )
    return posted


__all__ = [
    "consume_handoff_reviews",
    "parse_review_payload",
]
