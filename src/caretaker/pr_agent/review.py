"""Review comment handling for PRs.

The legacy :func:`classify_review_basic` walks a short keyword ladder
(``nit:`` / ``bug`` / ``must`` / trailing ``?``). T-A4 (Phase 2, §3.4)
adds an LLM-backed candidate that returns a structured
:class:`~caretaker.pr_agent.review_llm.ReviewClassification` with a
proper ``severity`` field; both paths run behind
``@shadow_decision("review_classification")`` so the mode knob on
:class:`~caretaker.config.AgenticConfig.review_classification` controls
which side is authoritative.

``ReviewAnalysis`` now carries a pass-through ``severity`` field so
downstream consumers (the Copilot bridge, the merge-readiness gate) can
branch on a blocker review without re-parsing the raw comment body.
Callers that only care about the legacy ``comment_type`` enum continue
to work unchanged.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from caretaker.evolution.shadow import shadow_decision
from caretaker.github_client.models import Review, ReviewState
from caretaker.identity import is_automated
from caretaker.pr_agent.review_llm import (
    ReviewClassification,
    ReviewSeverity,
    classify_review_comment_llm,
    classify_review_legacy_adapter,
    compare_classifications,
)

if TYPE_CHECKING:
    from caretaker.llm.router import LLMRouter

logger = logging.getLogger(__name__)


class ReviewVerdict(StrEnum):
    """Decision the PR agent takes after assessing review analyses."""

    APPROVE = "approve"  # No blocking findings → submit GitHub approval
    FIX = "fix"  # Fixable issues → dispatch to Copilot / Claude Code
    CLOSE = "close"  # Infeasible: duplicate, won't work, out of scope
    ESCALATE = "escalate"  # Too large / architectural / uncertain → human


# Keywords in review body that signal the proposed change is infeasible or
# duplicated and the PR should be closed rather than fixed.
_CLOSE_SIGNALS: frozenset[str] = frozenset(
    {
        "duplicate",
        "already exists",
        "already implemented",
        "already addressed",
        "already done",
        "already merged",
        "won't work",
        "will not work",
        "wont work",
        "not feasible",
        "infeasible",
        "not viable",
        "not possible",
        "out of scope",
        "not in scope",
        "not applicable",
        "not relevant",
        "low probability",
        "unlikely to succeed",
        "low chance of success",
        "this is unnecessary",
        "not needed",
    }
)

# Keywords that signal a blocker requiring human architectural judgement rather
# than a mechanical code fix.
_ESCALATE_SIGNALS: frozenset[str] = frozenset(
    {
        "significant refactor",
        "major refactor",
        "architectural change",
        "redesign",
        "requires design",
        "needs design",
        "complex change",
        "too large",
        "breaking change",
        "needs discussion",
        "needs rfc",
        "design doc",
    }
)


def assess_review_verdict(
    analyses: list[ReviewAnalysis],
    pr_additions: int = 0,
    *,
    high_loc_threshold: int = 500,
) -> tuple[ReviewVerdict, str]:
    """Decide what action to take given a list of review analyses.

    Decision ladder (first match wins):
    1. No analyses → APPROVE (nothing to fix)
    2. Any body contains a close signal → CLOSE
    3. Any body contains an escalate signal → ESCALATE
    4. Blocker severity on a high-LoC PR (> high_loc_threshold additions) → ESCALATE
    5. Otherwise → FIX (dispatch to Copilot / Claude Code)
    """
    if not analyses:
        return ReviewVerdict.APPROVE, "No blocking review findings"

    for analysis in analyses:
        body_lower = analysis.body.lower()
        if any(sig in body_lower for sig in _CLOSE_SIGNALS):
            return ReviewVerdict.CLOSE, f"Infeasible / duplicate: {analysis.summary[:120]}"
        if any(sig in body_lower for sig in _ESCALATE_SIGNALS):
            return (
                ReviewVerdict.ESCALATE,
                f"Architectural concern: {analysis.summary[:120]}",
            )
        if analysis.severity == "blocker" and pr_additions > high_loc_threshold:
            return (
                ReviewVerdict.ESCALATE,
                f"Blocker on high-LoC PR ({pr_additions} additions): {analysis.summary[:120]}",
            )

    return ReviewVerdict.FIX, "Fixable review comments"


class ReviewCommentType(StrEnum):
    ACTIONABLE = "ACTIONABLE"
    NITPICK = "NITPICK"
    QUESTION = "QUESTION"
    PRAISE = "PRAISE"
    UNKNOWN = "UNKNOWN"


@dataclass
class ReviewAnalysis:
    """Per-review verdict the PR agent / Copilot bridge consume.

    ``severity`` is a pass-through from the Phase 2
    :class:`ReviewClassification`; when the shadow decision runs under
    ``mode=off`` (the default) the legacy adapter sets it from the
    keyword ladder. Downstream code should branch on ``severity ==
    "blocker"`` rather than re-deriving it from the comment text —
    TODO: once ``mode=enforce`` is the default, collapse ``comment_type``
    and expose only the structured classification.
    """

    reviewer: str
    comment_type: ReviewCommentType
    summary: str
    complexity: str  # trivial, moderate, complex
    body: str
    severity: ReviewSeverity = "minor"
    classification: ReviewClassification | None = field(default=None, repr=False)


# ── Legacy → comment_type/complexity mapping ─────────────────────────────


# Translates the structured ``(kind, severity)`` pair back onto the legacy
# ``(comment_type, complexity)`` vocabulary the PR agent and Copilot
# bridge still consume. The round-trip is exact for the five legacy
# comment_type rows; new structured ``kinds`` (``discussion``, ``praise``
# on CHANGES_REQUESTED) fall through to ``ACTIONABLE``/``moderate`` so
# the downstream gate still fires on the review.
_KIND_TO_LEGACY_COMMENT_TYPE: dict[str, ReviewCommentType] = {
    "actionable": ReviewCommentType.ACTIONABLE,
    "nitpick": ReviewCommentType.NITPICK,
    "question": ReviewCommentType.QUESTION,
    "praise": ReviewCommentType.PRAISE,
    "discussion": ReviewCommentType.ACTIONABLE,
}


_SEVERITY_TO_COMPLEXITY: dict[str, str] = {
    "blocker": "complex",
    "major": "moderate",
    "minor": "moderate",
    "trivial": "trivial",
}


def _apply_classification(
    analysis: ReviewAnalysis,
    verdict: ReviewClassification,
) -> ReviewAnalysis:
    """Fold a :class:`ReviewClassification` into a legacy :class:`ReviewAnalysis`.

    Keeps ``body`` / ``reviewer`` unchanged. Overwrites ``comment_type``
    and ``complexity`` from the structured verdict so the downstream
    Copilot bridge + merge gate see a consistent view whether the legacy
    adapter or the LLM candidate produced the classification.
    """
    analysis.comment_type = _KIND_TO_LEGACY_COMMENT_TYPE.get(
        verdict.kind, ReviewCommentType.ACTIONABLE
    )
    analysis.complexity = _SEVERITY_TO_COMPLEXITY.get(verdict.severity, "moderate")
    analysis.severity = verdict.severity
    analysis.classification = verdict
    # Prefer the structured one-line summary when it's non-empty — the
    # legacy ``summary`` field is just the first 200 chars of the body.
    if verdict.summary_one_line:
        analysis.summary = verdict.summary_one_line
    return analysis


def classify_review_basic(review: Review) -> ReviewAnalysis:
    """Basic review classification using heuristics.

    Preserved verbatim for backward compatibility with tests and for the
    shadow-mode legacy branch. The severity pass-through field is
    populated from :func:`classify_review_legacy_adapter` so callers
    that only use the legacy helper still get a structured severity.
    """
    body_lower = review.body.lower()

    if review.state == ReviewState.APPROVED:
        analysis = ReviewAnalysis(
            reviewer=review.user.login,
            comment_type=ReviewCommentType.PRAISE,
            summary="Approval",
            complexity="trivial",
            body=review.body,
            severity="trivial",
        )
        analysis.classification = classify_review_legacy_adapter(analysis, review)
        return analysis

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

    analysis = ReviewAnalysis(
        reviewer=review.user.login,
        comment_type=comment_type,
        summary=summary,
        complexity=complexity,
        body=review.body,
    )
    # Attach the structured legacy verdict so the severity pass-through
    # reflects the keyword table documented in T-A4.
    structured = classify_review_legacy_adapter(analysis, review)
    analysis.severity = structured.severity
    analysis.classification = structured
    return analysis


# ── Shadow-wrapped classification dispatch ───────────────────────────────
#
# The decorator runs the legacy adapter + the LLM candidate under
# ``agentic.review_classification.mode=shadow`` and only the candidate
# under ``enforce``. ``compare_classifications`` ignores noisy fields
# (summary, suggested prompt) so small rewording doesn't spam the
# disagreement counter.


@shadow_decision("review_classification", compare=compare_classifications)
async def _dispatch_review_classification(
    *,
    legacy: Any,
    candidate: Any,
    context: Any = None,
) -> ReviewClassification:
    """Thin shim the decorator drives. The actual work lives in the
    ``legacy`` / ``candidate`` callables supplied by :func:`analyze_reviews`.
    """
    raise AssertionError(
        "@shadow_decision wrapper should dispatch via legacy/candidate"
    )  # pragma: no cover


async def _classify_one_review(
    review: Review,
    *,
    llm_router: LLMRouter | None,
    pr_title: str,
    repo_slug: str,
) -> ReviewAnalysis:
    """Run the shadow-wrapped classification for a single ``review``.

    Build the legacy verdict up-front (cheap, deterministic) so the
    decorator's ``legacy`` callable can hand it back without re-doing
    the work. The candidate lazily calls the LLM only when the router is
    wired and available — matching the pattern used by the readiness and
    CI-triage migrations.
    """
    legacy_analysis = classify_review_basic(review)
    legacy_verdict = classify_review_legacy_adapter(legacy_analysis, review)

    async def _legacy_fn() -> ReviewClassification:
        return legacy_verdict

    async def _candidate_fn() -> ReviewClassification | None:
        if llm_router is None or not getattr(llm_router, "claude_available", False):
            return None
        claude = llm_router.claude
        if claude is None or not getattr(claude, "available", False):
            return None
        return await classify_review_comment_llm(
            review,
            claude=claude,
            pr_title=pr_title,
        )

    try:
        verdict = await _dispatch_review_classification(
            legacy=_legacy_fn,
            candidate=_candidate_fn,
            context={
                "repo_slug": repo_slug,
                "reviewer": getattr(review.user, "login", ""),
                "review_state": getattr(review.state, "value", str(review.state)),
            },
        )
    except Exception as exc:  # noqa: BLE001 — defensive: never fail the agent
        logger.warning(
            "_classify_one_review: shadow-decision failed for @%s (%s: %s); "
            "falling back to legacy adapter verbatim",
            getattr(review.user, "login", "?"),
            type(exc).__name__,
            exc,
        )
        verdict = legacy_verdict

    return _apply_classification(legacy_analysis, verdict)


async def analyze_reviews(
    reviews: list[Review],
    nitpick_threshold: str = "low",
    llm_router: LLMRouter | None = None,
    *,
    pr_title: str = "",
    repo_slug: str = "",
) -> list[ReviewAnalysis]:
    """Analyze all blocking and automated-bot review comments.

    Each blocking review is run through the
    ``@shadow_decision("review_classification")`` dispatcher; in
    ``off`` / ``shadow`` modes the legacy keyword ladder is
    authoritative (behaviour unchanged from pre-T-A4), and in ``enforce``
    mode the LLM candidate wins with legacy acting as the safety net.
    """
    analyses: list[ReviewAnalysis] = []

    # Formal CHANGES_REQUESTED reviews always count as blocking.
    # COMMENTED reviews from automated reviewer bots also carry actionable feedback.
    actionable = [
        r
        for r in reviews
        if r.state == ReviewState.CHANGES_REQUESTED
        or (r.state == ReviewState.COMMENTED and r.body and is_automated(r.user.login))
    ]

    for review in actionable:
        analysis = await _classify_one_review(
            review,
            llm_router=llm_router,
            pr_title=pr_title,
            repo_slug=repo_slug,
        )
        analyses.append(analysis)

    # Filter by nitpick threshold — note: trivial-severity items are also
    # dropped on ``high`` so a discussion/trivial doesn't sneak through
    # when the nitpick kind bucket gets reclassified by the LLM.
    if nitpick_threshold == "high":
        analyses = [
            a
            for a in analyses
            if a.comment_type != ReviewCommentType.NITPICK and a.severity != "trivial"
        ]

    return analyses
