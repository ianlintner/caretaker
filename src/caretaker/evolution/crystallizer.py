"""SkillCrystallizer — writes skills back to InsightStore after verified outcomes.

Called from StateTracker.save() (and optionally from agent code) whenever a
TrackedPR transitions to MERGED or ESCALATED.  The crystallizer extracts the
problem category and signature from the PR's notes field and records the
outcome so the skill library stays current.

Phase 2 T-A10 migrates the category-inference path (but not the signature
extraction or the terminal-transition filter) onto the shared
:class:`~caretaker.pr_agent.ci_triage.FailureTriage` classifier. The
``@shadow_decision("crystallizer_category")`` wrapper lets us flip between
the legacy regex ladder (:func:`_infer_category`) and the LLM candidate
(:func:`_infer_category_llm`) from the ``agentic.crystallizer_category.mode``
config knob.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

from caretaker.evolution.insight_store import (
    CATEGORY_BUILD,
    CATEGORY_CI,
    CATEGORY_SECURITY,
    InsightStore,
)
from caretaker.evolution.shadow import shadow_decision
from caretaker.github_client.models import CheckConclusion, CheckRun, CheckStatus
from caretaker.pr_agent.ci_triage import (
    FailureCategory,
    FailureTriage,
    classify_failure_llm,
)
from caretaker.state.models import PRTrackingState, TrackedPR

if TYPE_CHECKING:
    from caretaker.llm.router import LLMRouter

logger = logging.getLogger(__name__)

# Regex patterns used to infer category from PR notes / CI failure text.
#
# Phase 2 T-A10: kept in place for the shadow rollout. Once
# ``agentic.crystallizer_category.mode`` has been at ``enforce`` long
# enough to show parity with the LLM classifier (tracked in the
# ``caretaker_shadow_decisions_total{name="crystallizer_category"}``
# counter), a follow-up PR can delete this table together with
# :func:`_infer_category`.
_CATEGORY_PATTERNS: list[tuple[str, str]] = [
    (CATEGORY_CI, r"jest|mocha|pytest|vitest|timeout|test|spec"),
    (CATEGORY_BUILD, r"build|webpack|tsc|typescript|compile|import|module"),
    (CATEGORY_SECURITY, r"secret|vuln|cve|dependabot|snyk"),
    (CATEGORY_CI, r"lint|eslint|ruff|flake8|mypy"),
]


# ── FailureTriage.category → InsightStore CATEGORY_* mapping ──────────────
#
# The LLM schema (see :class:`FailureTriage`) has a richer vocabulary than
# the InsightStore's three-value category enum. We collapse every
# FailureCategory onto one of ``CATEGORY_CI`` / ``CATEGORY_BUILD`` /
# ``CATEGORY_SECURITY``.
#
# TODO(T-A10, crystallizer_category): FailureTriage has no ``security``
# analogue, so the candidate can never produce ``CATEGORY_SECURITY``
# even when the legacy regex would (``secret``/``vuln``/``cve``/
# ``dependabot``/``snyk``). Those notes fall through to ``CATEGORY_CI``
# in the candidate path — shadow-mode disagreement records will surface
# how often this happens. If the rate is non-trivial, extend
# :data:`FailureCategory` with a ``security`` row before flipping
# ``mode=enforce``.
_FAILURE_CATEGORY_TO_STORE: dict[FailureCategory, str] = {
    "test": CATEGORY_CI,
    "lint": CATEGORY_CI,
    "type": CATEGORY_CI,
    "timeout": CATEGORY_CI,
    "build": CATEGORY_BUILD,
    # Infra / flaky / backpressure are all CI-flavoured in the legacy
    # crystallizer vocabulary — the skill library only cares about
    # "what family of fix works", not the transience of the failure.
    "flaky": CATEGORY_CI,
    "backpressure": CATEGORY_CI,
    "infra": CATEGORY_CI,
    "unknown": CATEGORY_CI,
}


def _infer_category(notes: str) -> str:
    """Infer failure category from free-text notes (legacy regex ladder).

    Kept as the legacy branch of :func:`_dispatch_category`. In
    ``agentic.crystallizer_category.mode=off`` (the default), this is the
    sole authority — behaviour is byte-identical to the pre-T-A10 world.

    A follow-up PR removes this function once shadow-mode data shows the
    LLM candidate agrees with the regex at an acceptable rate.
    """
    lower = notes.lower()
    for category, pattern in _CATEGORY_PATTERNS:
        if re.search(pattern, lower):
            return category
    return CATEGORY_CI


def _notes_to_check_run(notes: str) -> CheckRun:
    """Wrap free-text PR notes in a synthetic :class:`CheckRun`.

    The shared :func:`~caretaker.pr_agent.ci_triage.classify_failure_llm`
    classifier consumes :class:`CheckRun` / log-tail inputs. The
    crystallizer, by contrast, sees only the free-text notes field a
    previous agent wrote onto a :class:`TrackedPR`. We build a throw-away
    ``CheckRun`` so the LLM receives the same shape of prompt it does in
    the CI-triage path — this keeps the prompt cache key stable across
    the two call sites and avoids a second, subtly-different triage
    prompt we would then have to keep in sync.
    """
    return CheckRun(
        id=0,
        name="crystallizer",
        status=CheckStatus.COMPLETED,
        conclusion=CheckConclusion.FAILURE,
        output_summary=notes,
    )


def _map_triage_category(category: FailureCategory) -> str:
    """Translate a :data:`FailureCategory` into an InsightStore CATEGORY_*."""
    return _FAILURE_CATEGORY_TO_STORE.get(category, CATEGORY_CI)


async def _infer_category_llm(
    notes: str,
    *,
    llm_router: LLMRouter | None,
) -> str | None:
    """LLM candidate — reuses the T-A3 :class:`FailureTriage` classifier.

    Returns ``None`` when the LLM router isn't wired up or the candidate
    errors out so the shadow decorator falls through to the legacy regex
    without disrupting the hot path.
    """
    if llm_router is None or not llm_router.claude_available:
        return None
    claude = llm_router.claude
    if claude is None or not claude.available:
        return None

    check_run = _notes_to_check_run(notes)
    verdict: FailureTriage | None = await classify_failure_llm(
        check_run,
        notes,
        claude=claude,
    )
    if verdict is None:
        return None
    return _map_triage_category(verdict.category)


@shadow_decision("crystallizer_category")
async def _dispatch_category(
    *,
    legacy: Any,
    candidate: Any,
    context: Any = None,
) -> str:
    """Thin shim the shadow decorator drives.

    The decorator wires the legacy / candidate callables supplied by
    :meth:`SkillCrystallizer._infer_category_for_notes`; this body is
    never executed directly. See :mod:`caretaker.evolution.shadow` for
    the full contract.
    """
    raise AssertionError(
        "@shadow_decision wrapper should dispatch via legacy/candidate"
    )  # pragma: no cover


def _extract_signature(notes: str) -> str:
    """Extract a normalized signature from PR notes for skill keying.

    Strips PR-specific details (numbers, hashes, timestamps) so the same
    class of problem maps to the same signature across PRs.
    """
    # Remove PR-number-like tokens
    sig = re.sub(r"#\d+", "", notes)
    # Remove hex commit hashes
    sig = re.sub(r"\b[0-9a-f]{7,40}\b", "", sig)
    # Collapse whitespace
    sig = " ".join(sig.split()).strip()
    # Truncate to keep ID stable
    return sig[:120] if sig else "unknown"


class SkillCrystallizer:
    """Records skill outcomes when PRs are resolved.

    Usage — instantiate once per orchestrator run and call
    ``crystallize_transitions()`` before StateTracker.save() so it can compare
    the pre-save state snapshot against the post-agent state.
    """

    def __init__(
        self,
        insight_store: InsightStore,
        *,
        llm_router: LLMRouter | None = None,
    ) -> None:
        self._store = insight_store
        self._llm_router = llm_router

    async def crystallize_transitions(
        self,
        previous_prs: dict[int, TrackedPR],
        current_prs: dict[int, TrackedPR],
        *,
        repo_slug: str | None = None,
    ) -> int:
        """Compare old vs new PR states and crystallize outcomes.

        Returns the count of skills recorded.

        The category-inference step runs through
        :func:`_dispatch_category` so the
        ``agentic.crystallizer_category.mode`` flag can shadow or enforce
        the LLM classifier. ``off`` mode (the default) is byte-identical
        to the pre-T-A10 behaviour — the regex ladder is the sole
        authority and the LLM path is never invoked.
        """
        recorded = 0
        # Only MERGED / ESCALATED carry signal. CLOSED is often a human "abandon"
        # or the CI-backlog guard — neither tells us whether a fix worked.
        _terminal = {PRTrackingState.MERGED, PRTrackingState.ESCALATED}

        for pr_number, current in current_prs.items():
            if current.state not in _terminal:
                continue

            previous = previous_prs.get(pr_number)
            previous_state = previous.state if previous is not None else None

            # Only crystallize on genuine transitions to terminal states
            if previous_state == current.state:
                continue

            notes = current.notes or ""
            if not notes or notes in ("ci_backlog_guard", "closed:ci_backlog_guard"):
                continue

            category = await self._infer_category_for_notes(
                notes, pr_number=pr_number, repo_slug=repo_slug
            )
            signature = _extract_signature(notes)

            if current.state == PRTrackingState.MERGED:
                self._store.record_success(category, signature, sop=notes)
                logger.info(
                    "Crystallized success: PR #%d → category=%s sig='%.40s'",
                    pr_number,
                    category,
                    signature,
                )
                recorded += 1
            else:  # ESCALATED
                self._store.record_failure(category, signature)
                logger.debug(
                    "Crystallized failure: PR #%d → category=%s sig='%.40s'",
                    pr_number,
                    category,
                    signature,
                )
                recorded += 1

        return recorded

    async def _infer_category_for_notes(
        self,
        notes: str,
        *,
        pr_number: int,
        repo_slug: str | None,
    ) -> str:
        """Run the category inference through the shadow decorator."""
        router = self._llm_router

        async def _legacy() -> str:
            return _infer_category(notes)

        async def _candidate() -> str | None:
            return await _infer_category_llm(notes, llm_router=router)

        return await _dispatch_category(
            legacy=_legacy,
            candidate=_candidate,
            context={"repo_slug": repo_slug or "", "pr_number": pr_number},
        )
