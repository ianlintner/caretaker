"""LLM-backed PR readiness evaluation (Phase 2, §3.1 of the 2026-Q2 plan).

The legacy :func:`caretaker.pr_agent.states.evaluate_readiness` computes a
linear 10/20/30/40 score across mergeability, automated feedback, reviews,
and CI. That rubric pretends to be a formula but is really a classifier —
and on solo-maintainer repos the ``reviews_approved >= 1`` component can
never clear, so caretaker can never merge its own upgrade PRs.

This module introduces:

* :class:`Blocker` / :class:`Readiness` — pydantic v2 schema emitted by
  :meth:`caretaker.llm.claude.ClaudeClient.structured_complete`.
* :func:`readiness_from_legacy` — adapter that lifts the existing
  :class:`~caretaker.pr_agent.states.ReadinessEvaluation` onto the new
  :class:`Readiness` shape, so the legacy path and the LLM candidate can
  be compared byte-identically inside the ``@shadow_decision`` decorator.
* :func:`evaluate_pr_readiness_llm` — the LLM candidate. Builds a cache-
  friendly prompt (stable prefix, variable payload last) and calls
  ``structured_complete``. Any
  :class:`caretaker.llm.claude.StructuredCompleteError` yields ``None``
  so the shadow decorator falls through to the legacy adapter.

The call-site (``pr_agent/agent.py``) wires these two through
:func:`caretaker.evolution.shadow.shadow_decision` under the
``readiness`` decision name, so all three modes (off/shadow/enforce) are
controlled by ``AgenticConfig.readiness.mode`` without touching any other
caller.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field

from caretaker.llm.claude import StructuredCompleteError

if TYPE_CHECKING:
    from caretaker.github_client.models import CheckRun, PullRequest, Review
    from caretaker.llm.claude import ClaudeClient
    from caretaker.memory.retriever import MemoryRetriever
    from caretaker.pr_agent.states import ReadinessEvaluation

logger = logging.getLogger(__name__)


# ── Schema ───────────────────────────────────────────────────────────────

BlockerCategory = Literal[
    "ci_failing",
    "review_outstanding",
    "merge_conflict",
    "draft",
    "approval_required",
    "waiting_for_upstream",
    "policy_guard",
    "stale",
    "other",
]


class Blocker(BaseModel):
    """A single merge blocker attached to a :class:`Readiness` verdict.

    The ``category`` field is a closed enum so the shadow decorator can
    compare legacy vs. candidate verdicts without worrying about free-text
    drift. ``human_reason`` and ``suggested_action`` are free-form so the
    status-comment renderer can surface them verbatim.
    """

    category: BlockerCategory
    human_reason: str = Field(
        description="One-sentence actionable description shown in the status comment."
    )
    suggested_action: str = Field(
        description="Short imperative for the operator or bot to unblock the PR."
    )


Verdict = Literal["ready", "blocked", "pending", "needs_human"]


class Readiness(BaseModel):
    """Structured verdict emitted by the LLM and the legacy adapter.

    * ``ready`` — no blockers; the downstream merge gate can proceed.
    * ``blocked`` — hard blocker (CI failing, changes requested, draft,
      policy guard).
    * ``pending`` — soft blocker; wait and re-evaluate (CI pending,
      review requested but not yet submitted).
    * ``needs_human`` — escalation required; neither the PR author nor
      caretaker can resolve automatically.
    """

    verdict: Verdict
    confidence: float = Field(ge=0.0, le=1.0)
    blockers: list[Blocker] = Field(default_factory=list)
    summary: str = Field(max_length=200)


# ── Legacy adapter ───────────────────────────────────────────────────────

# Maps the legacy token strings emitted by
# :func:`caretaker.pr_agent.states.evaluate_readiness` onto the new
# :data:`BlockerCategory` enum. Tokens not in this table fall through to
# ``other`` so the adapter never raises on an unrecognised blocker.
_LEGACY_BLOCKER_MAP: dict[str, BlockerCategory] = {
    "ci_failing": "ci_failing",
    "ci_pending": "ci_failing",
    "changes_requested": "review_outstanding",
    "automated_feedback_unaddressed": "review_outstanding",
    "required_review_missing": "approval_required",
    "merge_conflict": "merge_conflict",
    "draft_pr": "draft",
    "breaking_change": "policy_guard",
    "manual_hold": "policy_guard",
}


_LEGACY_ACTION_HINTS: dict[str, str] = {
    "ci_failing": "Investigate the failing check runs and push a fix.",
    "ci_pending": "Wait for the in-progress check runs to complete.",
    "changes_requested": "Address the reviewer's requested changes and re-request review.",
    "automated_feedback_unaddressed": "Resolve the automated reviewer comments.",
    "required_review_missing": "Request a review from a repository maintainer.",
    "merge_conflict": "Rebase or merge the base branch and resolve conflicts.",
    "draft_pr": "Mark the pull request ready for review.",
    "breaking_change": "Confirm the breaking-change policy gate with a maintainer.",
    "manual_hold": "Remove the ``caretaker:hold`` label when the hold is resolved.",
}


_LEGACY_REASON_HINTS: dict[str, str] = {
    "ci_failing": "One or more required CI checks failed.",
    "ci_pending": "One or more required CI checks are still running.",
    "changes_requested": "A reviewer explicitly requested changes.",
    "automated_feedback_unaddressed": "An automated reviewer left actionable feedback.",
    "required_review_missing": "The repository requires at least one approving review.",
    "merge_conflict": "The PR branch conflicts with the base branch.",
    "draft_pr": "The PR is still in draft.",
    "breaking_change": "The PR is labelled ``maintainer:breaking``.",
    "manual_hold": "The PR is labelled ``caretaker:hold``.",
}


def _legacy_blocker_to_structured(token: str) -> Blocker:
    """Translate one legacy token string into a :class:`Blocker`."""
    category = _LEGACY_BLOCKER_MAP.get(token, "other")
    reason = _LEGACY_REASON_HINTS.get(token, f"Legacy blocker: {token}.")
    action = _LEGACY_ACTION_HINTS.get(token, "Review the PR and resolve the blocker.")
    return Blocker(
        category=category,
        human_reason=reason,
        suggested_action=action,
    )


def readiness_from_legacy(evaluation: ReadinessEvaluation) -> Readiness:
    """Lift a legacy :class:`ReadinessEvaluation` onto the :class:`Readiness` shape.

    Preserves the legacy blocker ordering so the deterministic path stays
    byte-identical when it is the authoritative verdict. The ``confidence``
    for the legacy path is derived from the score: 1.0 when there are no
    blockers, otherwise a proportional value.
    """
    blockers = [_legacy_blocker_to_structured(b) for b in evaluation.blockers]
    verdict: Verdict
    if evaluation.conclusion == "success":
        verdict = "ready"
    elif evaluation.conclusion == "in_progress":
        verdict = "pending"
    else:
        # ``failure`` covers both hard-blocked (merge conflict, changes
        # requested, breaking) and needs_human cases. The legacy rubric
        # cannot distinguish the two, so surface as ``blocked`` — operators
        # can still reclassify via the shadow-mode disagreement feed.
        verdict = "blocked"

    summary = (evaluation.summary or "").strip()
    # The schema caps summary at 200 chars; truncate defensively.
    if len(summary) > 200:
        summary = summary[:197] + "..."

    confidence = 1.0 if not evaluation.blockers else round(max(evaluation.score, 0.0), 2)
    return Readiness(
        verdict=verdict,
        confidence=confidence,
        blockers=blockers,
        summary=summary or "Legacy readiness adapter produced no summary.",
    )


# ── LLM candidate ────────────────────────────────────────────────────────


@dataclass
class PRReadinessContext:
    """Variable payload fed to the LLM candidate.

    Kept as a plain dataclass (rather than pydantic) so the call site can
    build it cheaply during the hot path; the only field that actually
    needs validation is the :class:`PullRequest`, which is pydantic.

    The prompt builder puts the stable prefix first and this payload last
    so Anthropic prompt caching hits on the prefix across every readiness
    evaluation in a run.
    """

    pr: PullRequest
    check_runs: list[CheckRun] = field(default_factory=list)
    reviews: list[Review] = field(default_factory=list)
    linked_issues: list[str] = field(default_factory=list)
    repo_slug: str = ""
    is_solo_maintainer: bool = False


_READINESS_SYSTEM_PROMPT = """\
You are caretaker's merge-readiness classifier. Given a pull request
snapshot, decide whether the PR is ready to merge right now.

Rules:
- Verdict must be one of: ready, blocked, pending, needs_human.
- Use ``pending`` when the only thing missing is progress (CI still
  running, review requested but not yet returned) — do NOT use
  ``blocked`` for those cases.
- Use ``blocked`` for hard blockers (CI failing, changes requested,
  merge conflict, draft, policy labels).
- Use ``needs_human`` only when neither the author nor caretaker can
  resolve the situation automatically (e.g. upstream dependency, policy
  debate).
- When ``is_solo_maintainer`` is true, do NOT require an approving
  review to mark the PR ``ready`` — the missing-review rubric does not
  apply on solo repos.
- Each ``blockers`` entry must pick the nearest ``category`` from the
  schema enum; fall back to ``other`` only when nothing else fits.
- ``summary`` must be a single line no longer than 200 characters.
- ``confidence`` is your self-assessed probability the verdict is correct.
"""


def _truncate(text: str, limit: int) -> str:
    """Trim ``text`` so it fits in ``limit`` characters, tagging truncations."""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


def _render_review_summary(review: Review) -> str:
    user = getattr(review.user, "login", "?")
    state = getattr(review.state, "value", str(review.state))
    body = _truncate((review.body or "").strip(), 400)
    if not body:
        return f"- @{user} ({state})"
    return f"- @{user} ({state}): {body}"


def _render_check_summary(run: CheckRun) -> str:
    status = getattr(run.status, "value", str(run.status))
    concl = getattr(run.conclusion, "value", "") if run.conclusion else ""
    suffix = f":{concl}" if concl else ""
    title = (run.output_title or "").strip()
    return f"- {run.name} [{status}{suffix}]" + (f" — {title}" if title else "")


def build_readiness_prompt(
    context: PRReadinessContext,
    *,
    memory_block: str | None = None,
) -> str:
    """Assemble the variable-payload prompt body.

    The stable prefix (the system prompt) is passed through to
    ``structured_complete`` as ``system=``, which is where Anthropic
    prompt caching looks for a cache-able prefix. Only the per-PR payload
    changes across calls; that's what this function renders.

    When ``memory_block`` is supplied (T-E2 cross-run retrieval), it is
    inserted at the head of the payload, before the variable PR facts.
    The block is rendered by
    :meth:`caretaker.memory.retriever.MemoryRetriever.format_for_prompt`
    under a hard token budget so the injection stays well-behaved even
    on repos with hundreds of prior dispatches.
    """
    pr = context.pr
    labels = ", ".join(label.name for label in pr.labels) or "(none)"
    body_snippet = _truncate((pr.body or "").strip(), 2000)
    reviews_block = (
        "\n".join(_render_review_summary(r) for r in context.reviews) or "(no reviews yet)"
    )
    checks_block = (
        "\n".join(_render_check_summary(c) for c in context.check_runs) or "(no check runs)"
    )
    linked_block = "\n".join(f"- {ref}" for ref in context.linked_issues) or "(none)"

    prefix = ""
    if memory_block:
        prefix = memory_block.rstrip() + "\n\n"

    return (
        f"{prefix}"
        "PR title: "
        f"{pr.title}\n"
        f"PR number: #{pr.number}\n"
        f"Repo: {context.repo_slug or '?'}\n"
        f"Draft: {pr.draft}\n"
        f"Mergeable: {pr.mergeable}\n"
        f"Labels: {labels}\n"
        f"Is solo maintainer repo: {context.is_solo_maintainer}\n"
        f"Linked issues:\n{linked_block}\n"
        f"Checks:\n{checks_block}\n"
        f"Reviews:\n{reviews_block}\n"
        f"PR body (truncated to 2000 chars):\n{body_snippet}\n"
    )


async def _build_memory_block_for_readiness(
    context: PRReadinessContext,
    retriever: MemoryRetriever,
) -> str:
    """Run the retriever for the readiness call and format its hits.

    Builds the ``query_text`` from the PR title, labels, and repo slug —
    the minimum signal the Jaccard fallback can meaningfully rank. When
    the retriever returns no hits the function yields an empty string so
    the caller skips the injection cleanly.
    """
    pr = context.pr
    label_names = " ".join(label.name for label in pr.labels)
    query = " ".join(part for part in (pr.title, label_names, context.repo_slug) if part)
    try:
        hits = await retriever.find_relevant(
            agent="pr_agent",
            query_text=query,
            repo_slug=context.repo_slug or None,
        )
    except Exception as exc:  # noqa: BLE001 - retrieval must never fail the readiness call
        logger.info(
            "_build_memory_block_for_readiness: retrieval failed for PR #%s: %s",
            pr.number,
            exc,
        )
        return ""
    return retriever.format_for_prompt(hits)


async def evaluate_pr_readiness_llm(
    context: PRReadinessContext,
    *,
    claude: ClaudeClient,
    retriever: MemoryRetriever | None = None,
) -> Readiness | None:
    """Call the LLM and return its :class:`Readiness` verdict, or ``None``.

    Returns ``None`` on any :class:`StructuredCompleteError` so the
    ``@shadow_decision`` wrapper can fall through to the legacy adapter.
    All other exceptions propagate — shadow mode swallows them and
    records a ``candidate_error`` event, enforce mode falls through.

    When ``retriever`` is supplied (T-E2 cross-run retrieval enabled),
    the retriever is queried for up to three prior memory snapshots
    matching the PR and the formatted block is inlined at the head of
    the prompt payload. Retrieval is best-effort: a failing retriever
    degrades to the no-memory prompt rather than failing the call.
    """
    memory_block: str | None = None
    if retriever is not None:
        memory_block = await _build_memory_block_for_readiness(context, retriever)

    prompt = build_readiness_prompt(context, memory_block=memory_block)
    try:
        return await claude.structured_complete(
            prompt,
            schema=Readiness,
            feature="pr_readiness",
            system=_READINESS_SYSTEM_PROMPT,
        )
    except StructuredCompleteError as exc:
        logger.info(
            "evaluate_pr_readiness_llm: structured_complete failed for PR #%s: %s",
            context.pr.number,
            exc,
        )
        return None


__all__ = [
    "Blocker",
    "BlockerCategory",
    "PRReadinessContext",
    "Readiness",
    "Verdict",
    "build_readiness_prompt",
    "evaluate_pr_readiness_llm",
    "readiness_from_legacy",
]
