"""LLM-backed stuck-PR detection (Phase 2 §3.8 of the 2026-Q2 plan).

The legacy :meth:`caretaker.pr_agent.agent.PRAgent._is_pr_stuck_by_age`
is a two-signal heuristic: PR open longer than ``stuck_age_hours`` *and*
no human approval on file. That fires correctly for the long-tail
abandonment cases (portfolio #4 / #28) but is blind to the three other
stuck-shapes observed in the fleet audit:

* **CI deadlock** — checks forever ``in_progress`` with no progress; no
  humans can unstick this, only an infrastructure nudge.
* **Awaiting human decision** — the PR is structurally ready but a
  maintainer has to choose between two acceptable paths (breaking-change
  gate, policy debate).
* **Solo-repo / no-reviewer** — P4/M3 pattern. The PR is ready but the
  repo has exactly one maintainer, who is also the author, so the
  ``required_review_missing`` blocker can never clear without
  self-approval.

This module introduces:

* :class:`StuckVerdict` — the pydantic v2 schema emitted by the LLM
  candidate and by the legacy adapter, so
  :func:`caretaker.evolution.shadow.shadow_decision` can compare both
  paths without a bespoke comparator.
* :func:`stuck_from_legacy` — lifts the binary
  :meth:`_is_pr_stuck_by_age` signal onto the richer :class:`StuckVerdict`
  shape with a stable ``recommended_action`` choice.
* :func:`evaluate_stuck_pr_llm` — the LLM candidate. Builds a stable
  cache-friendly prompt (system prefix, variable payload last) and calls
  ``structured_complete``. Any
  :class:`caretaker.llm.claude.StructuredCompleteError` yields ``None``
  so the shadow decorator falls through to the legacy adapter.

The wiring from :class:`~caretaker.pr_agent.agent.PRAgent._process_pr`
lives in ``pr_agent/agent.py``. A minimum-age pre-filter
(``PRAgentConfig.stuck_age_hours``) runs *before* either path so the
LLM is never called on freshly-opened PRs.
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
    from caretaker.pr_agent.readiness_llm import Readiness

logger = logging.getLogger(__name__)


# ── Schema ───────────────────────────────────────────────────────────────

StuckReason = Literal[
    "abandoned",
    "awaiting_human_decision",
    "ci_deadlock",
    "merge_queue",
    "solo_repo_no_reviewer",
    "not_stuck",
]
"""The six stuck-shapes the classifier distinguishes.

* ``abandoned`` — opened long ago, no progress; the legacy heuristic's
  bread-and-butter. Recommended follow-up is ``escalate`` or
  ``close_stale``.
* ``awaiting_human_decision`` — maintainer has to choose between two
  acceptable paths. Recommended follow-up is ``nudge_reviewer``.
* ``ci_deadlock`` — CI runs are perpetually in-progress or require an
  infrastructure approval. Recommended follow-up is usually ``wait`` or
  ``escalate``.
* ``merge_queue`` — structurally ready but sitting in a queue behind
  other PRs. Recommended follow-up is ``wait``.
* ``solo_repo_no_reviewer`` — the P4/M3 solo-maintainer pattern; the
  PR cannot clear ``required_review_missing`` because there is nobody
  else to approve it. Recommended follow-up is ``self_approve_on_solo``.
* ``not_stuck`` — baseline. Recommended follow-up is ``wait``.
"""


RecommendedAction = Literal[
    "escalate",
    "nudge_reviewer",
    "request_fix",
    "wait",
    "close_stale",
    "self_approve_on_solo",
]
"""Closed enum of the actions the caller can take on a stuck PR.

``self_approve_on_solo`` and ``close_stale`` are new — the legacy binary
gate only produced ``escalate`` / ``wait``. When the shadow decorator
runs in ``off`` / ``shadow`` modes the adapter picks between those two
so the disagreement signal is meaningful.
"""


class StuckVerdict(BaseModel):
    """Structured verdict for the stuck-PR decision site.

    Emitted by :func:`evaluate_stuck_pr_llm` and by
    :func:`stuck_from_legacy`. The schema is deliberately small — the
    shadow comparator only looks at ``is_stuck`` and
    ``recommended_action``, so keeping the other fields free-text avoids
    noisy disagreements on rewordings of the explanation.
    """

    is_stuck: bool
    stuck_reason: StuckReason
    recommended_action: RecommendedAction
    explanation: str = Field(max_length=300)
    confidence: float = Field(ge=0.0, le=1.0)


# ── Legacy adapter ───────────────────────────────────────────────────────


def stuck_from_legacy(is_stuck_by_age: bool) -> StuckVerdict:
    """Lift the binary age-heuristic verdict onto the :class:`StuckVerdict`.

    The legacy heuristic only emits two states — stuck-by-age (True)
    and not-stuck-by-age (False). We map those to ``abandoned`` /
    ``escalate`` and ``not_stuck`` / ``wait`` respectively so the
    shadow-mode comparator has something to chew on.

    The ``confidence`` is hard-coded at 0.5 — we have no evidence the
    age cutoff was correct, only that it triggered. Higher values would
    be dishonest.
    """
    if is_stuck_by_age:
        return StuckVerdict(
            is_stuck=True,
            stuck_reason="abandoned",
            recommended_action="escalate",
            explanation="Legacy heuristic: age > threshold with no human approval.",
            confidence=0.5,
        )
    return StuckVerdict(
        is_stuck=False,
        stuck_reason="not_stuck",
        recommended_action="wait",
        explanation="Legacy heuristic: no stuck-by-age signal.",
        confidence=0.5,
    )


# ── LLM candidate ────────────────────────────────────────────────────────


@dataclass
class PRStuckContext:
    """Variable payload for :func:`evaluate_stuck_pr_llm`.

    Kept as a plain dataclass (rather than pydantic) so call sites can
    build it cheaply in the hot path; the :class:`PullRequest` field is
    already pydantic-validated and everything else is primitive.

    The prompt builder places the system prefix first and this payload
    last so Anthropic prompt caching hits on the stable prefix across
    every stuck-PR evaluation in a run.
    """

    pr: PullRequest
    age_hours: float
    last_activity_hours: float | None = None
    check_runs: list[CheckRun] = field(default_factory=list)
    reviews: list[Review] = field(default_factory=list)
    readiness_verdict: Readiness | None = None
    linked_issues: list[str] = field(default_factory=list)
    repo_slug: str = ""
    collaborator_count: int | None = None


_STUCK_SYSTEM_PROMPT = """\
You are caretaker's stuck-PR classifier. Given a pull request snapshot,
decide whether the PR is stuck and — if so — why.

Rules:
- ``stuck_reason`` must be one of: abandoned, awaiting_human_decision,
  ci_deadlock, merge_queue, solo_repo_no_reviewer, not_stuck.
- ``recommended_action`` must be one of: escalate, nudge_reviewer,
  request_fix, wait, close_stale, self_approve_on_solo.
- ``is_stuck`` must be False iff ``stuck_reason`` is ``not_stuck``.
- Use ``solo_repo_no_reviewer`` (and ``self_approve_on_solo``) when the
  readiness verdict says ``ready`` and ``collaborator_count`` is 1 — the
  PR cannot clear ``required_review_missing`` because the repo has
  exactly one maintainer, who is almost certainly also the author.
- Use ``ci_deadlock`` when check runs have been ``in_progress`` for
  many hours with no recent transition, or when they require a
  workflow-approval action the author cannot perform.
- Use ``abandoned`` for long-open PRs whose last activity is stale and
  whose readiness verdict is not ``ready``.
- Use ``awaiting_human_decision`` when the readiness verdict is
  ``needs_human`` (policy gate, breaking-change, upstream dep) and the
  PR is otherwise structurally ready.
- Use ``merge_queue`` for PRs that are structurally ready but waiting
  behind a queue or a scheduled auto-merge window.
- ``close_stale`` is only appropriate when the PR has been abandoned
  for many days and the underlying change is no longer relevant (linked
  issues closed, base branch moved on).
- ``confidence`` is your self-assessed probability the verdict is
  correct. Prefer 0.5 when the signals conflict.
- ``explanation`` must be a single line no longer than 300 characters.
"""


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


def _render_review_summary(review: Review) -> str:
    user = getattr(review.user, "login", "?")
    state = getattr(review.state, "value", str(review.state))
    body = _truncate((review.body or "").strip(), 200)
    if not body:
        return f"- @{user} ({state})"
    return f"- @{user} ({state}): {body}"


def _render_check_summary(run: CheckRun) -> str:
    status = getattr(run.status, "value", str(run.status))
    concl = getattr(run.conclusion, "value", "") if run.conclusion else ""
    suffix = f":{concl}" if concl else ""
    title = (run.output_title or "").strip()
    return f"- {run.name} [{status}{suffix}]" + (f" — {title}" if title else "")


def build_stuck_pr_prompt(context: PRStuckContext) -> str:
    """Assemble the variable-payload prompt body.

    The stable prefix (system prompt) is passed through to
    ``structured_complete`` as ``system=``, which is where Anthropic
    prompt caching looks for a cache-able prefix. Only the per-PR payload
    changes across calls; that's what this function renders.
    """
    pr = context.pr
    labels = ", ".join(label.name for label in pr.labels) or "(none)"
    body_snippet = _truncate((pr.body or "").strip(), 1500)
    reviews_block = (
        "\n".join(_render_review_summary(r) for r in context.reviews) or "(no reviews yet)"
    )
    checks_block = (
        "\n".join(_render_check_summary(c) for c in context.check_runs) or "(no check runs)"
    )
    linked_block = "\n".join(f"- {ref}" for ref in context.linked_issues) or "(none)"
    last_activity = (
        f"{context.last_activity_hours:.1f}h ago"
        if context.last_activity_hours is not None
        else "unknown"
    )

    readiness_line = "(none)"
    if context.readiness_verdict is not None:
        readiness_line = (
            f"verdict={context.readiness_verdict.verdict} "
            f"summary={_truncate(context.readiness_verdict.summary, 200)}"
        )

    collaborator_line = (
        str(context.collaborator_count) if context.collaborator_count is not None else "unknown"
    )

    return (
        "PR title: "
        f"{pr.title}\n"
        f"PR number: #{pr.number}\n"
        f"Repo: {context.repo_slug or '?'}\n"
        f"Draft: {pr.draft}\n"
        f"Mergeable: {pr.mergeable}\n"
        f"Labels: {labels}\n"
        f"Age: {context.age_hours:.1f}h\n"
        f"Last activity: {last_activity}\n"
        f"Collaborator count: {collaborator_line}\n"
        f"Readiness verdict: {readiness_line}\n"
        f"Linked issues:\n{linked_block}\n"
        f"Checks:\n{checks_block}\n"
        f"Reviews:\n{reviews_block}\n"
        f"PR body (truncated to 1500 chars):\n{body_snippet}\n"
    )


async def evaluate_stuck_pr_llm(
    context: PRStuckContext,
    *,
    claude: ClaudeClient,
) -> StuckVerdict | None:
    """Call the LLM and return its :class:`StuckVerdict`, or ``None``.

    Returns ``None`` on any :class:`StructuredCompleteError` so the
    ``@shadow_decision`` wrapper can fall through to the legacy adapter.
    All other exceptions propagate — shadow mode swallows them and
    records a ``candidate_error`` event, enforce mode falls through.
    """
    prompt = build_stuck_pr_prompt(context)
    try:
        return await claude.structured_complete(
            prompt,
            schema=StuckVerdict,
            feature="pr_stuck",
            system=_STUCK_SYSTEM_PROMPT,
        )
    except StructuredCompleteError as exc:
        logger.info(
            "evaluate_stuck_pr_llm: structured_complete failed for PR #%s: %s",
            context.pr.number,
            exc,
        )
        return None


__all__ = [
    "PRStuckContext",
    "RecommendedAction",
    "StuckReason",
    "StuckVerdict",
    "build_stuck_pr_prompt",
    "evaluate_stuck_pr_llm",
    "stuck_from_legacy",
]
