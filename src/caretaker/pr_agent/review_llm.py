"""LLM-backed PR review-comment classification (Phase 2, §3.4).

The legacy :func:`caretaker.pr_agent.review.classify_review_basic` walks a
short keyword ladder (``nit:``, ``bug``, ``must``, trailing ``?``) and
buckets the reviewer's comment into :class:`ReviewCommentType`. That rubric
is both coarse (no severity, no suggested next action) and lossy — a
security ``must`` fix lands in the same ``ACTIONABLE`` bucket as a polite
``consider using a list comp``. The downstream Copilot prompt ends up
quoting the whole comment back at the model because caretaker has no
structured handle to reason about priority.

This module introduces:

* :class:`ReviewClassification` — pydantic v2 schema emitted by
  :meth:`caretaker.llm.claude.ClaudeClient.structured_complete`. Captures
  ``kind``, ``severity``, a one-line summary, a boolean on whether a code
  change is actually required, and an optional suggested prompt the
  Copilot bridge can feed verbatim.
* :func:`classify_review_comment_llm` — the candidate side of the shadow
  pair. Uses a stable system prompt (schema + rubric) and a variable user
  prompt (review body + PR title + optional diff hunk) so Anthropic's
  prompt cache hits on the prefix across every review classification.
* :func:`classify_review_legacy_adapter` — lifts the legacy
  :class:`caretaker.pr_agent.review.ReviewAnalysis` onto the
  :class:`ReviewClassification` shape using the explicit table documented
  in T-A4: ``nit:`` → nitpick/trivial, ``must`` / ``blocker`` →
  actionable/blocker, ``bug`` → actionable/major, trailing ``?`` →
  question/trivial, else discussion/minor. ``requires_code_change`` is
  ``True`` only when ``kind == "actionable"``; the legacy path has no way
  to synthesise a suggested prompt, so that field is always ``None`` on
  the legacy branch.
* :func:`compare_classifications` — shadow-mode comparator. Only
  ``(kind, severity)`` count; the LLM rewording in ``summary_one_line``
  and ``suggested_prompt_to_copilot`` is expected to drift and would
  otherwise spam the disagreement counter.

Wiring: :func:`caretaker.pr_agent.review.analyze_reviews` dispatches each
review through ``@shadow_decision("review_classification")`` so all three
modes (off / shadow / enforce) are controlled by
``AgenticConfig.review_classification.mode`` without touching the rest of
the review pipeline.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field

from caretaker.llm.claude import StructuredCompleteError

if TYPE_CHECKING:
    from caretaker.github_client.models import Review
    from caretaker.llm.claude import ClaudeClient
    from caretaker.pr_agent.review import ReviewAnalysis

logger = logging.getLogger(__name__)


# ── Schema ───────────────────────────────────────────────────────────────


ReviewKind = Literal["actionable", "nitpick", "question", "praise", "discussion"]
ReviewSeverity = Literal["blocker", "major", "minor", "trivial"]


class ReviewClassification(BaseModel):
    """Structured verdict for a single PR review comment.

    ``kind`` buckets the comment by intent; ``severity`` ranks the urgency
    so a ``blocker`` review can gate merge while a ``trivial`` nitpick
    does not. ``requires_code_change`` is the one field downstream code
    should branch on when deciding whether to dispatch a Copilot fix —
    ``False`` for praise / questions / discussion even when the reviewer
    typed a lot.

    ``suggested_prompt_to_copilot`` is an optional one-paragraph prompt
    the LLM can hand back so the Copilot bridge doesn't have to re-derive
    instructions from the raw comment body. Capped at 500 chars so it
    can't dominate the Copilot task.
    """

    kind: ReviewKind
    severity: ReviewSeverity
    summary_one_line: str = Field(max_length=140)
    requires_code_change: bool
    suggested_prompt_to_copilot: str | None = Field(default=None, max_length=500)


# ── Legacy adapter ───────────────────────────────────────────────────────


# Match order matters: ``must`` / ``blocker`` outrank ``bug``, which
# outranks ``nit:``, which outranks trailing ``?``. The existing ladder
# in :func:`classify_review_basic` already leans on substring precedence,
# so we preserve that ordering here.
_BLOCKER_TOKENS = ("must ", "blocker", " must", "must:")
_MAJOR_TOKENS = ("bug",)
_NITPICK_TOKENS = ("nit:", "nitpick", "optional:")


def _legacy_kind_severity(review: Review) -> tuple[ReviewKind, ReviewSeverity]:
    """Derive the ``(kind, severity)`` pair from a legacy :class:`Review`.

    Table (per T-A4 scope):

    * ``nit:`` / ``nitpick`` / ``optional:`` → nitpick / trivial
    * ``must`` / ``blocker`` → actionable / blocker
    * ``bug`` → actionable / major
    * trailing ``?`` → question / trivial
    * else → discussion / minor

    Approved reviews are handled separately in
    :func:`classify_review_legacy_adapter` and never reach this helper.
    """
    body = review.body or ""
    body_lower = body.lower()

    if any(tok in body_lower for tok in _BLOCKER_TOKENS):
        return "actionable", "blocker"
    if any(tok in body_lower for tok in _MAJOR_TOKENS):
        return "actionable", "major"
    if any(tok in body_lower for tok in _NITPICK_TOKENS):
        return "nitpick", "trivial"
    if body.strip().endswith("?"):
        return "question", "trivial"
    return "discussion", "minor"


def classify_review_legacy_adapter(
    analysis: ReviewAnalysis,
    review: Review,
) -> ReviewClassification:
    """Lift a legacy :class:`ReviewAnalysis` onto :class:`ReviewClassification`.

    Keeps the one-line summary from the analysis verbatim (truncated to
    the schema's 140-char cap). Approved reviews collapse to
    ``kind=praise``/``severity=trivial`` so the shadow-mode comparator
    has a real verdict to compare against — a praise comment never
    requires a code change even when it happens to contain the word
    ``bug`` (e.g. "nice bug catch!").

    The ``suggested_prompt_to_copilot`` field is ``None`` on the legacy
    branch by design: the heuristic path has no synthesis step, so
    surfacing a placeholder would mislead the Copilot bridge into
    thinking the LLM candidate wrote something useful.
    """
    from caretaker.github_client.models import ReviewState
    from caretaker.pr_agent.review import ReviewCommentType

    raw_summary = (analysis.summary or "").strip() or (review.body or "").strip()
    summary = raw_summary if len(raw_summary) <= 140 else raw_summary[:137] + "..."
    if not summary:
        summary = f"Review by @{analysis.reviewer}"

    # Approved reviews are praise/trivial regardless of body content.
    if review.state == ReviewState.APPROVED or analysis.comment_type == ReviewCommentType.PRAISE:
        return ReviewClassification(
            kind="praise",
            severity="trivial",
            summary_one_line=summary,
            requires_code_change=False,
            suggested_prompt_to_copilot=None,
        )

    kind, severity = _legacy_kind_severity(review)
    return ReviewClassification(
        kind=kind,
        severity=severity,
        summary_one_line=summary,
        requires_code_change=(kind == "actionable"),
        suggested_prompt_to_copilot=None,
    )


# ── Shadow-mode comparator ───────────────────────────────────────────────


def compare_classifications(a: ReviewClassification, b: ReviewClassification) -> bool:
    """Shadow-mode comparator: agree iff ``(kind, severity)`` match.

    The one-line summary is expected to drift (legacy uses the first 200
    chars of the body; the LLM paraphrases), and ``suggested_prompt`` is
    always ``None`` on the legacy side — counting either as a
    disagreement would drown the real signal. ``requires_code_change`` is
    a pure function of ``kind`` in the legacy adapter, so tracking it in
    the comparator too would double-count the same decision.
    """
    try:
        return (a.kind, a.severity) == (b.kind, b.severity)
    except AttributeError:
        return False


# ── LLM candidate ────────────────────────────────────────────────────────


_REVIEW_CLASSIFICATION_SYSTEM_PROMPT = """\
You are caretaker's PR review-comment classifier. Given a single review
comment (and optionally the PR title and the diff hunk the reviewer was
looking at), decide how to bucket it.

Rules:
- ``kind`` must be one of: actionable, nitpick, question, praise, discussion.
  * ``actionable`` — the reviewer is requesting a concrete code change.
  * ``nitpick`` — an optional style/readability suggestion that should not
    block merge.
  * ``question`` — the reviewer is asking for clarification, not a change.
  * ``praise`` — the reviewer is approving or complimenting the change.
  * ``discussion`` — open-ended commentary (design trade-off, follow-up
    idea) with no specific code change requested.
- ``severity`` must be one of: blocker, major, minor, trivial.
  * ``blocker`` — the PR should not merge until this is addressed
    (security issue, correctness bug, broken interface).
  * ``major`` — a real bug or missing test that should be fixed before
    merge but is not a security/correctness showstopper.
  * ``minor`` — a small improvement that should probably be addressed
    but does not need to block.
  * ``trivial`` — optional tweak; safe to defer or close as wontfix.
- ``requires_code_change`` is true iff caretaker should dispatch a
  follow-up Copilot task to edit code. Questions, praise, and
  discussions are false even when the reviewer wrote a lot.
- ``summary_one_line`` is a single sentence ≤140 chars, plain English,
  no markdown. Quote the reviewer's intent, not your own opinion.
- ``suggested_prompt_to_copilot`` is an optional one-paragraph prompt
  (≤500 chars) the Copilot bridge can feed verbatim to the fix executor.
  Only populate it when ``requires_code_change`` is true; leave null
  otherwise.
"""


def _truncate(text: str, limit: int) -> str:
    """Trim ``text`` to ``limit`` characters, tagging truncations."""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


def build_review_prompt(
    review: Review,
    *,
    pr_title: str = "",
    diff_hunk: str | None = None,
) -> str:
    """Render the user-turn prompt for :func:`classify_review_comment_llm`.

    Exposed for testing and for reviewers who want to eyeball what we
    actually send to the model. The stable system prompt (the rubric) is
    passed separately via ``system=`` so Anthropic prompt caching hits
    on the prefix — only this variable payload changes per-call.
    """
    reviewer_login = getattr(review.user, "login", "unknown")
    review_state = getattr(review.state, "value", str(review.state))
    body = _truncate((review.body or "").strip(), 4000)
    if not body:
        body = "(empty review body)"
    body_indented = "    " + body.replace("\n", "\n    ")

    hunk_block = ""
    if diff_hunk:
        hunk_trimmed = _truncate(diff_hunk.strip(), 2000)
        hunk_indented = "    " + hunk_trimmed.replace("\n", "\n    ")
        hunk_block = f"Diff hunk the reviewer was looking at:\n{hunk_indented}\n"

    title_block = f"PR title: {pr_title}\n" if pr_title else ""

    return (
        f"{title_block}"
        f"Reviewer: @{reviewer_login}\n"
        f"Review state: {review_state}\n"
        f"{hunk_block}"
        f"Review body:\n{body_indented}\n"
    )


async def classify_review_comment_llm(
    review: Review,
    *,
    claude: ClaudeClient,
    pr_title: str = "",
    diff_hunk: str | None = None,
) -> ReviewClassification | None:
    """Ask the LLM to classify ``review``; return ``None`` on any failure.

    The signature matches the Phase 2 candidate-function convention:
    keyword-only inputs, and a ``... | None`` return so the
    ``@shadow_decision`` wrapper can treat ``None`` as a candidate
    failure and fall through to the legacy adapter.

    Errors raised by :func:`ClaudeClient.structured_complete` are logged
    and converted to ``None``; any other exception is also caught so the
    candidate never takes down the hot path.
    """
    prompt = build_review_prompt(review, pr_title=pr_title, diff_hunk=diff_hunk)
    try:
        return await claude.structured_complete(
            prompt,
            schema=ReviewClassification,
            feature="review_classification",
            system=_REVIEW_CLASSIFICATION_SYSTEM_PROMPT,
        )
    except StructuredCompleteError as exc:
        logger.info(
            "classify_review_comment_llm: structured_complete failed for reviewer=%s: %s",
            getattr(review.user, "login", "?"),
            exc,
        )
        return None
    except Exception as exc:  # noqa: BLE001 — candidate must never raise up
        logger.warning(
            "classify_review_comment_llm: unexpected error for reviewer=%s: %s",
            getattr(review.user, "login", "?"),
            exc,
        )
        return None


__all__ = [
    "ReviewClassification",
    "ReviewKind",
    "ReviewSeverity",
    "build_review_prompt",
    "classify_review_comment_llm",
    "classify_review_legacy_adapter",
    "compare_classifications",
]
