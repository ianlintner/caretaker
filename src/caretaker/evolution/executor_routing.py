"""LLM-backed executor routing decision (Phase 2, §3.9 of the 2026-Q2 plan).

Replaces the two point systems that historically chose between inline LLM
review, the claude-code-action hand-off, the Foundry executor, and Copilot:

* :mod:`caretaker.pr_reviewer.routing` — LOC + file count + sensitive-path
  regex for PR-review routing (inline vs claude_code).
* :mod:`caretaker.foundry.size_classifier` — cheap pre/post-flight gates
  that decide Foundry vs Copilot.

Both rubrics claim to be formulas but are really classifiers: the score
thresholds drift against repo practice, the regex list rots, and neither
system can explain why it picked the path it did beyond listing the rules
that matched. That cost is what the migration is buying down.

This module introduces:

* :class:`ExecutorRoute` — pydantic v2 schema emitted by both the LLM
  candidate and the legacy adapters. The schema is small and closed so
  the shadow decorator can compare legacy vs. candidate on the
  authoritative ``path`` field without free-text drift.
* :class:`ExecutorRouteContext` — the variable payload the LLM candidate
  sees. Built once at the call site and fed in last so the stable prefix
  (the system prompt) stays cache-friendly.
* :func:`route_executor_llm` — the LLM candidate. Builds a cache-friendly
  prompt and calls ``structured_complete``. Any
  :class:`caretaker.llm.claude.StructuredCompleteError` yields ``None``
  so ``@shadow_decision`` falls through to the legacy adapter.
* :func:`route_from_pr_reviewer_legacy` — adapts a legacy
  :class:`caretaker.pr_reviewer.routing.RoutingDecision` onto
  :class:`ExecutorRoute` (path=``inline`` or ``claude_code``).
* :func:`route_from_foundry_legacy` — adapts a legacy
  :class:`caretaker.foundry.size_classifier.ClassifierResult` onto
  :class:`ExecutorRoute` (path=``foundry`` or ``copilot``).

The two call sites in ``pr_reviewer/agent.py`` and
``foundry/executor.py`` (via the dispatcher) both wrap their decision
with ``@shadow_decision("executor_routing")`` — the same decorator name
— so shadow data aggregates across them in a single ``executor_routing``
disagreement feed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field

from caretaker.llm.claude import StructuredCompleteError
from caretaker.pr_reviewer.routing import _SENSITIVE_PATTERNS

if TYPE_CHECKING:
    from caretaker.foundry.size_classifier import ClassifierResult
    from caretaker.llm.claude import ClaudeClient
    from caretaker.pr_reviewer.routing import RoutingDecision

logger = logging.getLogger(__name__)


# ── Schema ───────────────────────────────────────────────────────────────

ExecutorPath = Literal["inline", "foundry", "claude_code", "copilot"]
"""Closed set of executor destinations.

* ``inline`` — synchronous inline LLM review (pr-reviewer fast path).
* ``foundry`` — caretaker's self-owned tool-loop executor (Foundry).
* ``claude_code`` — claude-code-action hand-off (GitHub Action dispatch).
* ``copilot`` — legacy Copilot task comment (the ultimate fallback).
"""

RiskTag = Literal[
    "workflows_touched",
    "auth_touched",
    "migration_touched",
    "public_api_touched",
    "large_diff",
    "cross_package",
    "security_review_needed",
    "safe",
]
"""Structured risk tags attached to a route decision.

Kept as a closed enum so the shadow decorator can compare tag sets
without free-text drift. ``security_review_needed`` is the catch-all
for anything a human reviewer should eyeball regardless of executor
choice.
"""


class ExecutorRoute(BaseModel):
    """Structured routing verdict emitted by both the LLM and legacy adapters.

    ``path`` is the authoritative field for shadow-mode comparison — the
    ``reason`` and ``confidence`` fields carry observability metadata but
    are not part of the equality check. ``risk_tags`` is a *set-like*
    list (order is not significant); callers treat it as a set.
    """

    path: ExecutorPath
    reason: str = Field(max_length=300)
    risk_tags: list[RiskTag] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)


# ── LLM candidate context ────────────────────────────────────────────────


@dataclass
class ExecutorRouteFile:
    """One file entry fed into the routing prompt.

    Kept as a plain dataclass (rather than pydantic) so call sites can
    build it cheaply from either a GitHub API payload or a Foundry
    ``CodingTask`` without a second validation pass.
    """

    path: str
    additions: int = 0
    deletions: int = 0


@dataclass
class ExecutorRouteContext:
    """Variable payload for :func:`route_executor_llm`.

    The prompt builder places the stable prefix (system prompt) first and
    this payload last so Anthropic prompt caching hits on the prefix
    across every routing call in a run.

    Fields are deliberately small: the routing decision is a classifier,
    not a reviewer, so the LLM never needs raw diff content.
    """

    # What kind of task is being routed. For PRs this is the PR action
    # (e.g. ``opened``/``synchronize``); for Foundry it's the Copilot
    # task-type enum value. Free text so the prompt can handle both.
    task_type: str = ""
    # Files touched, with per-file LOC counts. Foundry may populate this
    # from the post-flight diff stats; pr_reviewer populates it from the
    # PR file list.
    files: list[ExecutorRouteFile] = field(default_factory=list)
    # Labels on the host PR / issue. Used as a soft signal — the LLM can
    # use them but should not be bound by them.
    labels: list[str] = field(default_factory=list)
    # Repo identity ("owner/repo") for log attribution and prompt context.
    repo_slug: str = ""
    # Candidate paths the caller is willing to consider. ``inline`` +
    # ``claude_code`` for pr_reviewer, ``foundry`` + ``copilot`` for the
    # dispatcher. Empty means "any".
    candidate_paths: list[ExecutorPath] = field(default_factory=list)
    # Short free-text description of the change, when available (PR title
    # / issue title / task summary). Optional — omitted when caller has
    # nothing cheap to hand.
    title: str = ""
    # A short body excerpt (already truncated by the caller). Optional.
    body: str = ""


# ── LLM prompt ───────────────────────────────────────────────────────────


_ROUTING_SYSTEM_PROMPT = """\
You are caretaker's executor router. Given a task or PR snapshot, pick
the cheapest executor that is *still safe* for the change and explain
your reasoning in one short sentence.

Rules:
- ``path`` must be one of: inline, foundry, claude_code, copilot.
  When the caller restricts candidate_paths, choose from that set only.
- Prefer ``inline`` for small, non-sensitive code review where a single
  LLM review is sufficient.
- Prefer ``foundry`` for small, well-scoped coding tasks (XS/S, few
  files, narrow blast radius).
- Prefer ``claude_code`` or ``copilot`` for complex or sensitive
  changes — anything touching CI workflows, auth/secrets, database
  migrations, public APIs, or many packages.
- Populate ``risk_tags`` with the closed-enum tags that apply:
  * ``workflows_touched`` — any file under ``.github/workflows/``.
  * ``auth_touched``      — secrets, tokens, credentials, auth code.
  * ``migration_touched`` — alembic / schema migration files.
  * ``public_api_touched``— exported interfaces, SDKs, public handlers.
  * ``large_diff``        — total additions + deletions > 400, or > 20
    files.
  * ``cross_package``     — changes span more than 3 top-level dirs.
  * ``security_review_needed`` — anything you are uncertain about or
    that warrants a human eyeball before merge.
  * ``safe``              — none of the above apply.
- ``reason`` is a single line no longer than 300 characters.
- ``confidence`` is your self-assessed probability the route is correct.
"""


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


def _detect_sensitive_hints(paths: list[str]) -> list[str]:
    """Return sensitive-path regex hints matched by ``paths``.

    Reuses the legacy sensitivity regex table as a *signal* to the LLM,
    not as an authoritative decision. The hints are echoed back in the
    prompt so the model can cite the same evidence the legacy rubric
    used without us re-implementing the regex ourselves.
    """
    hints: list[str] = []
    seen: set[str] = set()
    for path in paths:
        for pattern, _pts in _SENSITIVE_PATTERNS:
            key = pattern.pattern
            if key in seen:
                continue
            if pattern.search(path):
                hints.append(key)
                seen.add(key)
    return hints


def build_routing_prompt(context: ExecutorRouteContext) -> str:
    """Assemble the variable-payload prompt body.

    The stable prefix is passed through to ``structured_complete`` as
    ``system=`` — that's where Anthropic prompt caching looks for a
    cache-able prefix. Only the per-task payload changes across calls;
    that's what this function renders.
    """
    paths = [f.path for f in context.files]
    total_additions = sum(f.additions for f in context.files)
    total_deletions = sum(f.deletions for f in context.files)
    top_dirs = sorted({p.split("/")[0] for p in paths if "/" in p})

    files_block = (
        "\n".join(f"- {f.path} (+{f.additions}/-{f.deletions})" for f in context.files)
        or "(no files listed)"
    )
    labels_block = ", ".join(context.labels) or "(none)"
    candidates_block = ", ".join(context.candidate_paths) or "any"
    sensitive_hints = _detect_sensitive_hints(paths)
    hints_block = "\n".join(f"- {h}" for h in sensitive_hints) or "(none)"
    body_snippet = _truncate(context.body.strip(), 1000)

    return (
        f"Task type: {context.task_type or '?'}\n"
        f"Repo: {context.repo_slug or '?'}\n"
        f"Title: {context.title or '?'}\n"
        f"Candidate paths: {candidates_block}\n"
        f"Labels: {labels_block}\n"
        f"File count: {len(context.files)}\n"
        f"Total additions: {total_additions}\n"
        f"Total deletions: {total_deletions}\n"
        f"Top-level dirs touched ({len(top_dirs)}): "
        f"{', '.join(top_dirs) if top_dirs else '(none)'}\n"
        f"Sensitive-path hints (from legacy regex table):\n{hints_block}\n"
        f"Files:\n{files_block}\n"
        f"Body (truncated to 1000 chars):\n{body_snippet}\n"
    )


async def route_executor_llm(
    context: ExecutorRouteContext,
    *,
    claude: ClaudeClient,
) -> ExecutorRoute | None:
    """Call the LLM and return its :class:`ExecutorRoute`, or ``None``.

    Returns ``None`` on any :class:`StructuredCompleteError` so the
    ``@shadow_decision`` wrapper can fall through to the legacy adapter.
    All other exceptions propagate — shadow mode swallows them and
    records a ``candidate_error`` event, enforce mode falls through.
    """
    prompt = build_routing_prompt(context)
    try:
        return await claude.structured_complete(
            prompt,
            schema=ExecutorRoute,
            feature="executor_routing",
            system=_ROUTING_SYSTEM_PROMPT,
        )
    except StructuredCompleteError as exc:
        logger.info(
            "route_executor_llm: structured_complete failed (%s)",
            exc,
        )
        return None


# ── Legacy adapters ──────────────────────────────────────────────────────


def _infer_risk_tags(
    *,
    file_paths: list[str],
    additions: int,
    deletions: int,
    file_count: int,
) -> list[RiskTag]:
    """Derive :class:`RiskTag` entries from the inputs the legacy point
    systems already see.

    The mapping intentionally matches how the LLM is told to think about
    the tags in ``_ROUTING_SYSTEM_PROMPT`` so the shadow-mode comparison
    has a hope of agreeing on tag sets when both paths see the same
    signals.
    """
    tags: list[RiskTag] = []
    seen: set[RiskTag] = set()

    def add(tag: RiskTag) -> None:
        if tag not in seen:
            seen.add(tag)
            tags.append(tag)

    sensitive_matched = False
    for path in file_paths:
        lower = path.lower()
        if ".github/workflows/" in lower:
            add("workflows_touched")
            sensitive_matched = True
            continue
        if any(token in lower for token in ("secret", "credential", "auth", "token", "password")):
            add("auth_touched")
            sensitive_matched = True
            continue
        if any(token in lower for token in ("migration", "alembic", "schema")):
            add("migration_touched")
            sensitive_matched = True
            continue

    if "workflows_touched" in seen or "auth_touched" in seen:
        add("security_review_needed")

    total_lines = additions + deletions
    if total_lines > 400 or file_count > 20:
        add("large_diff")

    top_dirs: set[str] = set()
    for p in file_paths:
        if "/" in p:
            top_dirs.add(p.split("/")[0])
    if len(top_dirs) > 3:
        add("cross_package")

    if not tags and not sensitive_matched:
        add("safe")

    return tags


def route_from_pr_reviewer_legacy(
    decision: RoutingDecision,
    *,
    additions: int = 0,
    deletions: int = 0,
    file_count: int = 0,
    file_paths: list[str] | None = None,
) -> ExecutorRoute:
    """Lift a legacy :class:`RoutingDecision` onto :class:`ExecutorRoute`.

    Maps the pr_reviewer score-threshold categories:

    * ``use_inline == True``  → :attr:`ExecutorRoute.path` == ``"inline"``.
    * ``use_inline == False`` → :attr:`ExecutorRoute.path` == ``"claude_code"``.

    The ``reason`` string preserves the legacy score + triggered rules
    verbatim so the shadow-mode disagreement feed can show exactly what
    the point system was thinking.
    """
    path: ExecutorPath = "inline" if decision.use_inline else "claude_code"
    risk_tags = _infer_risk_tags(
        file_paths=file_paths or [],
        additions=additions,
        deletions=deletions,
        file_count=file_count,
    )
    reason = f"Legacy routing: score={decision.score}, {decision.reason}"
    # confidence: high when far from threshold, sagging as we get closer.
    distance = abs(decision.score - 40)
    confidence = max(0.5, min(1.0, 0.5 + (distance / 100.0)))
    return ExecutorRoute(
        path=path,
        reason=_truncate(reason, 300),
        risk_tags=risk_tags,
        confidence=round(confidence, 2),
    )


def route_from_foundry_legacy(
    result: ClassifierResult,
    *,
    additions: int = 0,
    deletions: int = 0,
    file_count: int = 0,
    file_paths: list[str] | None = None,
) -> ExecutorRoute:
    """Lift a legacy Foundry :class:`ClassifierResult` onto :class:`ExecutorRoute`.

    Maps the classifier decisions:

    * :attr:`Decision.ROUTE_FOUNDRY`    → path=``"foundry"``.
    * :attr:`Decision.ESCALATE_COPILOT` → path=``"copilot"``.
    * :attr:`Decision.ABORT`            → path=``"copilot"`` (caller
      still has to refuse, but the route verdict is "not us"; the
      shadow record captures the abort reason in ``reason``).
    """
    # Local import avoids a module-level cycle with foundry.size_classifier.
    from caretaker.foundry.size_classifier import Decision

    if result.decision == Decision.ROUTE_FOUNDRY:
        path: ExecutorPath = "foundry"
        triggered = "route_foundry"
    else:
        path = "copilot"
        triggered = "escalate_copilot" if result.decision == Decision.ESCALATE_COPILOT else "abort"

    risk_tags = _infer_risk_tags(
        file_paths=file_paths or [],
        additions=additions,
        deletions=deletions,
        file_count=file_count,
    )
    reason = f"Legacy routing: score=N, triggered rules=[{triggered}]: {result.reason}"
    # Legacy classifier is a pure rule engine, so it is confidently right
    # inside its scope. Confidence is 0.9 — a deliberate shade under 1.0
    # so the shadow comparison can surface ties to the LLM's verdict.
    return ExecutorRoute(
        path=path,
        reason=_truncate(reason, 300),
        risk_tags=risk_tags,
        confidence=0.9,
    )


# ── Shadow compare helper ───────────────────────────────────────────────


def executor_routes_agree(a: ExecutorRoute, b: ExecutorRoute) -> bool:
    """Compare two :class:`ExecutorRoute` verdicts at the decision level.

    Only the ``path`` field matters for shadow-mode disagreement
    accounting — ``reason``/``confidence`` drift between the legacy
    adapter and the LLM, and ``risk_tags`` ordering is insignificant.
    Agreement on ``path`` is what unblocks enforce-mode rollout.
    """
    return a.path == b.path


__all__ = [
    "ExecutorPath",
    "ExecutorRoute",
    "ExecutorRouteContext",
    "ExecutorRouteFile",
    "RiskTag",
    "build_routing_prompt",
    "executor_routes_agree",
    "route_executor_llm",
    "route_from_foundry_legacy",
    "route_from_pr_reviewer_legacy",
]
