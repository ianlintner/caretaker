"""Cheap LLM classifier for PR complexity.

Outputs a 4-level complexity tier that downstream callers (review,
auto-fix) use to pick the right model. Designed to be very cheap:
defaults to Gemini Flash-Lite via OpenRouter (~$0.0001 per call) so
running it on every PR doesn't dominate the bill the actual review
work is supposed to dominate.

A fast deterministic pre-filter handles obviously-trivial PRs (tiny
diffs, no sensitive paths, lint/typo labels) without an LLM call —
~30% of PRs in typical bot fleets short-circuit here.

Tiers
-----
* ``trivial`` — typo/comment fixes, formatting, single-line tweaks,
  bot lint commits. Cheapest model is fine; deterministic_lint often
  wins outright.
* ``simple``  — small bug fix, isolated test fix, < 50 LOC, one or
  two files, no sensitive paths. Cheap model is enough.
* ``standard``— ordinary feature work, 50-300 LOC, narrow blast
  radius. Mid-tier model.
* ``complex`` — large refactors, security-sensitive, > 300 LOC,
  workflows / migrations / auth touched. Top-tier model.

Reuses :class:`ExecutorRouteContext` from ``evolution.executor_routing``
so the prompt context shape is identical across the routing and
classification call sites.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field

from caretaker.evolution.executor_routing import ExecutorRouteContext, _detect_sensitive_hints
from caretaker.llm.claude import StructuredCompleteError

if TYPE_CHECKING:
    from caretaker.llm.claude import ClaudeClient
    from caretaker.pr_reviewer.routing import RoutingDecision

logger = logging.getLogger(__name__)


ComplexityTier = Literal["trivial", "simple", "standard", "complex"]


class ComplexityVerdict(BaseModel):
    """LLM-emitted PR complexity classification."""

    tier: ComplexityTier
    reason: str = Field(max_length=200)
    confidence: float = Field(ge=0.0, le=1.0)


# Labels that strongly signal trivial work — short-circuit the LLM.
_TRIVIAL_LABELS: frozenset[str] = frozenset(
    {
        "chore",
        "docs",
        "documentation",
        "typo",
        "good-first-issue",
        "lint",
        "format",
        "style",
        "caretaker:auto-fix",
    }
)

# Labels that strongly signal complex work — short-circuit the LLM.
_COMPLEX_LABELS: frozenset[str] = frozenset(
    {
        "architecture",
        "needs-prd",
        "breaking-change",
        "refactor",
        "migration",
        "security",
    }
)


_CLASSIFIER_SYSTEM_PROMPT = """\
You are caretaker's PR complexity classifier. Given a PR snapshot, pick
the cheapest tier that is still safe and accurate for the change.

Tiers (most to least cheap):
- ``trivial``  — typo/comment fixes, formatting, single-line tweaks,
                 lint-only changes. < 10 LOC, no logic shift.
- ``simple``   — small isolated fix (one bug, one test fix), < 50 LOC,
                 1-2 files, no sensitive paths.
- ``standard`` — ordinary feature work, 50-300 LOC, a few related files,
                 narrow blast radius.
- ``complex``  — large refactors, > 300 LOC, > 10 files, OR any change
                 touching CI workflows, auth/secrets, DB migrations,
                 public APIs, or cross-package surfaces.

Rules:
- When in doubt between two tiers, pick the higher (more capable) one.
  Misclassifying complex as simple is much worse than the reverse.
- A change can be "simple" by LOC but "complex" by sensitivity — the
  highest applicable tier wins.
- ``reason`` is one short sentence (< 200 chars).
- ``confidence`` is your self-assessed probability the tier is correct.
"""


def _build_classifier_prompt(context: ExecutorRouteContext) -> str:
    """Render the variable payload — system prompt is the cache-able prefix."""
    paths = [f.path for f in context.files]
    total_additions = sum(f.additions for f in context.files)
    total_deletions = sum(f.deletions for f in context.files)
    top_dirs = sorted({p.split("/")[0] for p in paths if "/" in p})
    sensitive_hints = _detect_sensitive_hints(paths)
    hints_block = ", ".join(sensitive_hints) or "(none)"
    files_block = (
        "\n".join(f"- {f.path} (+{f.additions}/-{f.deletions})" for f in context.files[:30])
        or "(no files listed)"
    )
    extra = ""
    if len(context.files) > 30:
        extra = f"\n... and {len(context.files) - 30} more files"
    labels_block = ", ".join(context.labels) or "(none)"

    return (
        f"Repo: {context.repo_slug or '?'}\n"
        f"Title: {context.title or '?'}\n"
        f"Labels: {labels_block}\n"
        f"File count: {len(context.files)}\n"
        f"Total LOC: +{total_additions}/-{total_deletions}\n"
        f"Top-level dirs ({len(top_dirs)}): "
        f"{', '.join(top_dirs) if top_dirs else '(none)'}\n"
        f"Sensitive-path hints: {hints_block}\n"
        f"Files:\n{files_block}{extra}\n"
    )


def fast_path_tier(
    *,
    context: ExecutorRouteContext,
    routing_decision: RoutingDecision | None = None,
) -> ComplexityTier | None:
    """Return a tier without calling the LLM, or ``None`` to defer.

    Order matters — check complex signals first so a tiny diff that
    touches ``.github/workflows/`` still escalates. Returns ``None``
    when the heuristic is uncertain; callers fall back to the LLM.
    """
    paths = [f.path for f in context.files]
    total_loc = sum(f.additions + f.deletions for f in context.files)
    labels = {label.lower() for label in context.labels}

    # Sensitive path → never trivial. Touching workflows or auth code
    # is "complex" no matter how small the diff.
    if _detect_sensitive_hints(paths):
        return "complex"
    if labels & _COMPLEX_LABELS:
        return "complex"

    # Tiny + clearly trivial label → trivial. The LLM would agree but
    # we save the call.
    if total_loc <= 10 and len(context.files) <= 2:
        return "trivial"
    if total_loc <= 30 and (labels & _TRIVIAL_LABELS):
        return "trivial"

    # Routing decision can short-circuit too: a very low score means
    # the heuristic already decided it's small + safe.
    if routing_decision is not None and routing_decision.score < 5:
        return "trivial"

    # Defer everything else to the LLM.
    return None


async def classify(
    *,
    context: ExecutorRouteContext,
    claude: ClaudeClient | None,
    routing_decision: RoutingDecision | None = None,
) -> ComplexityVerdict:
    """Classify a PR's complexity, using the fast path when possible.

    Always returns a :class:`ComplexityVerdict`. On LLM failure or
    unavailability, falls back to a heuristic verdict so callers can
    continue without a special-case branch.
    """
    fast = fast_path_tier(context=context, routing_decision=routing_decision)
    if fast is not None:
        return ComplexityVerdict(tier=fast, reason="heuristic fast path", confidence=0.9)

    if claude is None or not getattr(claude, "available", True):
        # No LLM available; pick a safe tier from heuristics.
        return _heuristic_fallback(context, reason="LLM unavailable")

    prompt = _build_classifier_prompt(context)
    try:
        return await claude.structured_complete(
            prompt,
            schema=ComplexityVerdict,
            feature="complexity_classifier",
            system=_CLASSIFIER_SYSTEM_PROMPT,
            max_tokens=300,
        )
    except StructuredCompleteError as exc:
        logger.info("complexity_classifier: structured_complete failed (%s)", exc)
        return _heuristic_fallback(context, reason=f"LLM error: {exc}")
    except Exception as exc:  # noqa: BLE001 — never break the caller on a classifier hiccup
        logger.warning(
            "complexity_classifier: unexpected error (%s); falling back to heuristic",
            exc,
        )
        return _heuristic_fallback(context, reason=f"classifier exception: {exc}")


def _heuristic_fallback(context: ExecutorRouteContext, *, reason: str) -> ComplexityVerdict:
    """Pick a tier from raw signals when the LLM can't produce one.

    Conservative: prefer a higher tier on ambiguity so we don't drop a
    risky PR onto a Flash-Lite model.
    """
    paths = [f.path for f in context.files]
    total_loc = sum(f.additions + f.deletions for f in context.files)
    if _detect_sensitive_hints(paths):
        tier: ComplexityTier = "complex"
    elif total_loc > 300 or len(context.files) > 10:
        tier = "complex"
    elif total_loc > 50 or len(context.files) > 2:
        tier = "standard"
    elif total_loc > 10:
        tier = "simple"
    else:
        tier = "trivial"
    return ComplexityVerdict(tier=tier, reason=reason, confidence=0.6)


__all__ = [
    "ComplexityTier",
    "ComplexityVerdict",
    "classify",
    "fast_path_tier",
]
