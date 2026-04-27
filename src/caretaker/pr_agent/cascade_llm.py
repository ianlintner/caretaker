"""LLM-backed cross-entity cascade decisions (Phase 2, §3.6 of the 2026-Q2 plan).

The deterministic planners in :mod:`caretaker.pr_agent.cascade` handle the
rule-driven paths byte-identically — ``on_pr_merged`` emits a
``CLOSE_ISSUE`` for every open linked issue and ``parse_linked_issues``
strictly matches ``Fixes #N`` / ``Closes #N`` keywords. Those stay as-is.

What this module migrates are the two *judgement* sites:

1. **on_issue_closed_as_duplicate** — when issue #A is closed as a
   duplicate of canonical #B, what should happen to every open PR whose
   body references #A? The legacy heuristic always emits a redirect
   comment, which is mostly right but can't tell a substantive PR (keep
   it open, retarget by hand) from a pointless scaffold PR (close it).
2. **The "short body + single linked issue -> close PR" rule** — this
   is the same judgement as (1), just on the auto-close axis. The cut-
   off at 200 chars is a proxy for "does this PR have real work in it".

Both sites now run behind :func:`caretaker.evolution.shadow.shadow_decision`
under the ``cascade`` name:

* :class:`CascadeDecision` — closed-enum schema the model emits.
* :func:`decide_cascade_llm` — the LLM candidate.
* :func:`cascade_decision_from_legacy_*` — per-rule adapters that lift
  the existing sync planner output onto :class:`CascadeDecision` so the
  shadow decorator can compare verdicts.

The :func:`on_issue_closed_as_duplicate` sync planner stays available for
callers that don't want the shadow gate (and for unit tests that assert
the deterministic path); the shadow-aware async entry point is
:func:`plan_on_issue_closed_as_duplicate`.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from caretaker.evolution.shadow import shadow_decision
from caretaker.llm.claude import StructuredCompleteError
from caretaker.pr_agent.cascade import (
    CascadeAction,
    CascadeKind,
    parse_linked_issues,
)

if TYPE_CHECKING:
    from caretaker.github_client.models import Issue
    from caretaker.llm.claude import ClaudeClient
    from caretaker.state.models import TrackedPR

logger = logging.getLogger(__name__)


# ── Schema ───────────────────────────────────────────────────────────────

CascadeActionLiteral = Literal[
    "redirect",
    "close_pr",
    "close_issue",
    "keep_open",
    "unlink",
]


class CascadeDecision(BaseModel):
    """Structured verdict emitted by the LLM and the legacy adapter.

    The action vocabulary is a closed enum so the ``@shadow_decision``
    decorator's ``compare`` predicate can decide agreement on the action
    field alone — ``justification`` drift (free-form rationale) must not
    count as a disagreement. Values:

    * ``redirect`` — leave the PR open but post a comment pointing at
      the canonical issue (the legacy "comment on PR" action).
    * ``close_pr`` — the PR is pointless now that its target issue is
      gone (the legacy "short body + single linked issue" heuristic).
    * ``close_issue`` — the linked issue should close (only emitted by
      the ``on_pr_merged`` path, which stays rule-driven and is NOT
      routed through this decision; included for schema completeness).
    * ``keep_open`` — no cascade action; the PR retains substance even
      without its original issue.
    * ``unlink`` — clear the issue's ``assigned_pr`` pointer so the next
      triage pass re-queues it (the legacy ``on_pr_closed_unmerged``
      action; also rule-driven and NOT routed here).
    """

    model_config = ConfigDict(extra="forbid")

    action: CascadeActionLiteral
    justification: str = Field(
        max_length=300,
        description=(
            "One-sentence explanation surfaced in the cascade audit trail "
            "and in any comment the executor posts on the target."
        ),
    )
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Self-assessed probability the action is correct.",
    )


# ── Event context ────────────────────────────────────────────────────────

CascadeEventType = Literal[
    "on_pr_merged",
    "on_pr_closed_unmerged",
    "on_issue_closed_as_duplicate",
]


class CascadeEventContext(BaseModel):
    """Variable payload fed to the LLM candidate.

    Pydantic (not a dataclass) so the schema round-trips through the
    shadow-decision record when verdicts are persisted for audit. The
    ``pr_body`` is pre-truncated by the caller to keep prompt size bounded
    and prevent runaway cost on large PR descriptions.
    """

    model_config = ConfigDict(extra="forbid")

    event_type: CascadeEventType
    pr_number: int
    pr_title: str = ""
    pr_body: str = Field(default="", max_length=4000)
    pr_draft: bool = False
    linked_issues: list[int] = Field(default_factory=list)
    closed_issue_number: int | None = Field(
        default=None,
        description="Issue number being closed (for duplicate / merge events).",
    )
    canonical_issue_number: int | None = Field(
        default=None,
        description="Canonical duplicate target when event_type is 'on_issue_closed_as_duplicate'.",
    )
    repo_slug: str = ""


# ── LLM candidate ────────────────────────────────────────────────────────

_CASCADE_SYSTEM_PROMPT = """\
You are caretaker's cross-entity cascade planner. Given a PR and the event
that triggered the cascade (an issue it referenced was closed as a
duplicate of another, or the PR itself was merged / closed), decide what
should happen to the PR.

Rules:
- ``action`` must be one of: redirect, close_pr, close_issue, keep_open,
  unlink. Pick the single most appropriate action.
- Use ``redirect`` when the PR still has substantive work in its body and
  the canonical issue is a reasonable retarget (the human should decide
  whether to rebase or retarget the PR).
- Use ``close_pr`` ONLY when the PR body is effectively a placeholder or
  references only the now-closed issue with no real description of the
  change being made.
- Use ``keep_open`` when the PR has enough substance that it should
  continue even without its original linked issue (e.g. implements a
  feature whose scope extends beyond the duplicate).
- Use ``close_issue`` / ``unlink`` only if the event_type clearly calls
  for it (merged PR / closed-unmerged PR). Those paths are typically
  handled by deterministic rules; prefer one of the other actions here.
- ``justification`` must be a single sentence no longer than 300 chars.
- ``confidence`` is your self-assessed probability the verdict is correct;
  be conservative (<= 0.7) when the PR body is ambiguous.
"""


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


def build_cascade_prompt(context: CascadeEventContext) -> str:
    """Assemble the variable-payload prompt body.

    The stable system prompt is passed through as ``system=`` so Anthropic
    prompt caching can reuse the prefix across every cascade decision in
    a single run; only this per-event body changes.
    """
    linked = ", ".join(f"#{n}" for n in context.linked_issues) or "(none)"
    closed = f"#{context.closed_issue_number}" if context.closed_issue_number else "(n/a)"
    canonical = f"#{context.canonical_issue_number}" if context.canonical_issue_number else "(n/a)"
    body_snippet = _truncate((context.pr_body or "").strip(), 2000)
    return (
        f"Event type: {context.event_type}\n"
        f"Repo: {context.repo_slug or '?'}\n"
        f"PR number: #{context.pr_number}\n"
        f"PR title: {context.pr_title}\n"
        f"PR draft: {context.pr_draft}\n"
        f"PR linked issues: {linked}\n"
        f"Closed issue: {closed}\n"
        f"Canonical duplicate target: {canonical}\n"
        f"PR body (truncated to 2000 chars):\n{body_snippet}\n"
    )


async def decide_cascade_llm(
    event_context: CascadeEventContext,
    *,
    claude: ClaudeClient,
) -> CascadeDecision | None:
    """Call the LLM and return its :class:`CascadeDecision`, or ``None``.

    Returns ``None`` on any :class:`StructuredCompleteError` so the
    ``@shadow_decision`` wrapper falls through to the legacy adapter.
    """
    prompt = build_cascade_prompt(event_context)
    try:
        return await claude.structured_complete(
            prompt,
            schema=CascadeDecision,
            feature="cascade_decision",
            system=_CASCADE_SYSTEM_PROMPT,
        )
    except StructuredCompleteError as exc:
        logger.info(
            "decide_cascade_llm: structured_complete failed for PR #%s (%s): %s",
            event_context.pr_number,
            event_context.event_type,
            exc,
        )
        return None


# ── Legacy adapters ──────────────────────────────────────────────────────
#
# Each adapter lifts one of the deterministic rule sites onto a
# :class:`CascadeDecision`. Confidence is 1.0 because the legacy rule is
# deterministic by construction; disagreement in shadow mode therefore
# cleanly reflects an LLM-vs-rule judgement gap.


def cascade_decision_from_redirect_rule(
    *, canonical_issue_number: int, closed_issue_number: int
) -> CascadeDecision:
    """Adapter for the legacy ``COMMENT_ON_PR`` (redirect) rule."""
    return CascadeDecision(
        action="redirect",
        justification=(
            "Legacy rule: comment_on_pr_redirect — issue "
            f"#{closed_issue_number} closed as duplicate of "
            f"#{canonical_issue_number}; PR may need retargeting."
        ),
        confidence=1.0,
    )


def cascade_decision_from_close_pr_rule(
    *,
    canonical_issue_number: int,
    closed_issue_number: int,
) -> CascadeDecision:
    """Adapter for the legacy ``CLOSE_PR`` (short-body heuristic) rule."""
    return CascadeDecision(
        action="close_pr",
        justification=(
            "Legacy rule: close_pr_short_body — target issue "
            f"#{closed_issue_number} closed as duplicate of "
            f"#{canonical_issue_number}; PR body is short with no other "
            "linked issues, treating as a pointless scaffold PR."
        ),
        confidence=1.0,
    )


def cascade_decision_from_keep_open_rule(
    *, pr_number: int, closed_issue_number: int
) -> CascadeDecision:
    """Adapter for "no close" — the rule judged the PR body substantial."""
    return CascadeDecision(
        action="keep_open",
        justification=(
            f"Legacy rule: keep_open — PR #{pr_number} body is substantive "
            f"(>=200 chars or multiple linked issues); not closing even "
            f"though issue #{closed_issue_number} closed as duplicate."
        ),
        confidence=1.0,
    )


# ── Shadow-decision bridges ──────────────────────────────────────────────


def _decisions_agree(a: Any, b: Any) -> bool:
    """Compare two :class:`CascadeDecision` verdicts at the action level.

    None-safe: when either side is ``None`` (the candidate returned
    ``None`` instead of raising) the comparison is a clean disagreement
    rather than an ``AttributeError``. Non-``CascadeDecision`` values are
    also handled gracefully — the compare must never raise, since a raise
    inside the decorator bubbles past the shadow-mode safety net.
    """
    if a is None or b is None:
        return a is None and b is None
    a_action = getattr(a, "action", None)
    b_action = getattr(b, "action", None)
    if a_action is None or b_action is None:
        return False
    return bool(a_action == b_action)


@shadow_decision("cascade", compare=_decisions_agree)
async def _decide_redirect(*, legacy: Any, candidate: Any, context: Any = None) -> CascadeDecision:
    """Shadow gate for the "should we redirect this PR?" decision.

    Body never runs — the decorator short-circuits to ``legacy`` in
    ``off`` / ``shadow`` modes and to ``candidate`` in ``enforce``.
    """
    raise AssertionError("shadow_decision wrapper short-circuits this placeholder")


@shadow_decision("cascade", compare=_decisions_agree)
async def _decide_close_pr(*, legacy: Any, candidate: Any, context: Any = None) -> CascadeDecision:
    """Shadow gate for the "should we close this PR?" decision."""
    raise AssertionError("shadow_decision wrapper short-circuits this placeholder")


# ── Async planner for on_issue_closed_as_duplicate ───────────────────────


async def plan_on_issue_closed_as_duplicate(
    issue: Issue,
    canonical_issue_number: int,
    tracked_prs: dict[int, TrackedPR],
    pr_bodies: dict[int, str] | None = None,
    *,
    claude: ClaudeClient | None = None,
    repo_slug: str = "",
) -> list[CascadeAction]:
    """Shadow-aware async variant of ``on_issue_closed_as_duplicate``.

    Deterministic inputs (``parse_linked_issues``, the set of tracked PRs)
    stay rule-driven. Two judgement sites are routed through the
    ``cascade`` shadow gate:

    1. **Redirect decision** — does this PR still need the redirect
       comment? Legacy says "yes, always"; the LLM may say ``keep_open``
       when the PR is tangential.
    2. **Close-PR decision** — is the PR body pointless now? Legacy uses
       a 200-char heuristic; the LLM can read the body.

    In ``off`` / ``shadow`` modes the legacy rule verdict is authoritative
    and the call-site behaviour is byte-identical to the sync planner. In
    ``enforce`` mode the LLM verdict wins and falls through to legacy on
    candidate error.
    """
    actions: list[CascadeAction] = []
    pr_bodies = pr_bodies or {}

    for pr_number, _tracked in tracked_prs.items():
        body = pr_bodies.get(pr_number, "")
        linked = parse_linked_issues(body)
        if issue.number not in linked:
            continue

        stripped = body.strip()
        legacy_should_close = len(stripped) < 200 and len(linked) == 1

        # Build the shared candidate context once; each decision gate
        # reuses it so we only call the LLM twice per PR at most.
        event_ctx = CascadeEventContext(
            event_type="on_issue_closed_as_duplicate",
            pr_number=pr_number,
            pr_body=_truncate(body, 4000),
            linked_issues=linked,
            closed_issue_number=issue.number,
            canonical_issue_number=canonical_issue_number,
            repo_slug=repo_slug,
        )

        # ── Decision 1: redirect comment? ─────────────────────────────

        async def _redirect_legacy() -> CascadeDecision:
            return cascade_decision_from_redirect_rule(
                canonical_issue_number=canonical_issue_number,
                closed_issue_number=issue.number,
            )

        async def _redirect_candidate(
            ctx: CascadeEventContext = event_ctx,
        ) -> CascadeDecision | None:
            if claude is None:
                return None
            return await decide_cascade_llm(ctx, claude=claude)

        try:
            redirect_verdict = await _decide_redirect(
                legacy=_redirect_legacy,
                candidate=_redirect_candidate,
                context={
                    "pr_number": pr_number,
                    "repo_slug": repo_slug,
                    "decision_site": "redirect",
                    "closed_issue_number": issue.number,
                },
            )
        except Exception as exc:  # noqa: BLE001 — never fail cascade planning
            logger.warning(
                "cascade redirect shadow-decision failed for PR #%d (%s: %s); "
                "falling back to legacy rule",
                pr_number,
                type(exc).__name__,
                exc,
            )
            redirect_verdict = cascade_decision_from_redirect_rule(
                canonical_issue_number=canonical_issue_number,
                closed_issue_number=issue.number,
            )

        if redirect_verdict.action == "redirect":
            actions.append(
                CascadeAction(
                    kind=CascadeKind.COMMENT_ON_PR,
                    target=pr_number,
                    reason=(
                        f"Issue #{issue.number} was closed as a duplicate of "
                        f"#{canonical_issue_number}. This PR may need to be "
                        f"retargeted."
                    ),
                    source=canonical_issue_number,
                )
            )

        # ── Decision 2: close the PR? ────────────────────────────────

        async def _close_legacy(
            should_close: bool = legacy_should_close,
            pr_number_bound: int = pr_number,
        ) -> CascadeDecision:
            if should_close:
                return cascade_decision_from_close_pr_rule(
                    canonical_issue_number=canonical_issue_number,
                    closed_issue_number=issue.number,
                )
            return cascade_decision_from_keep_open_rule(
                pr_number=pr_number_bound,
                closed_issue_number=issue.number,
            )

        async def _close_candidate(
            ctx: CascadeEventContext = event_ctx,
        ) -> CascadeDecision | None:
            if claude is None:
                return None
            return await decide_cascade_llm(ctx, claude=claude)

        try:
            close_verdict = await _decide_close_pr(
                legacy=_close_legacy,
                candidate=_close_candidate,
                context={
                    "pr_number": pr_number,
                    "repo_slug": repo_slug,
                    "decision_site": "close_pr",
                    "closed_issue_number": issue.number,
                },
            )
        except Exception as exc:  # noqa: BLE001 — never fail cascade planning
            logger.warning(
                "cascade close_pr shadow-decision failed for PR #%d (%s: %s); "
                "falling back to legacy rule",
                pr_number,
                type(exc).__name__,
                exc,
            )
            close_verdict = await _close_legacy()

        if close_verdict.action == "close_pr":
            actions.append(
                CascadeAction(
                    kind=CascadeKind.CLOSE_PR,
                    target=pr_number,
                    reason=(
                        f"Target issue #{issue.number} closed as duplicate of "
                        f"#{canonical_issue_number}; no substantive work in PR body."
                    ),
                    source=canonical_issue_number,
                )
            )

    return actions


__all__ = [
    "CascadeActionLiteral",
    "CascadeDecision",
    "CascadeEventContext",
    "CascadeEventType",
    "build_cascade_prompt",
    "cascade_decision_from_close_pr_rule",
    "cascade_decision_from_keep_open_rule",
    "cascade_decision_from_redirect_rule",
    "decide_cascade_llm",
    "plan_on_issue_closed_as_duplicate",
]
