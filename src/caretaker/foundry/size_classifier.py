"""Pre- and post-flight eligibility gates for the Foundry executor.

The classifier is intentionally simple: cheap heuristics on metadata, no
LLM call.  Its purpose is to keep Foundry focused on XS/S/SM tasks and
escalate anything larger to Copilot before wasting tokens or — post-flight —
before pushing an overly large diff.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel, Field


class Decision(StrEnum):
    """What the classifier says to do with a task."""

    ROUTE_FOUNDRY = "ROUTE_FOUNDRY"
    ESCALATE_COPILOT = "ESCALATE_COPILOT"
    ABORT = "ABORT"


@dataclass
class ClassifierResult:
    decision: Decision
    reason: str


def pre_flight(
    *,
    task_type: str,
    allowed_task_types: list[str],
    head_repo_full_name: str | None,
    base_repo_full_name: str | None,
    route_same_repo_only: bool,
    error_output: str,
    max_error_output_chars: int = 16_000,
) -> ClassifierResult:
    """Decide whether a task is eligible for Foundry routing.

    Called before opening a workspace — so it must only inspect metadata, not
    the repository state.
    """
    if task_type not in allowed_task_types:
        return ClassifierResult(
            decision=Decision.ESCALATE_COPILOT,
            reason=f"task_type {task_type!r} not in allowlist {allowed_task_types}",
        )

    if route_same_repo_only:
        # When either side is unknown we stay conservative and escalate.
        if not head_repo_full_name or not base_repo_full_name:
            return ClassifierResult(
                decision=Decision.ESCALATE_COPILOT,
                reason="head/base repo identity unknown (fork check cannot be confirmed)",
            )
        if head_repo_full_name != base_repo_full_name:
            return ClassifierResult(
                decision=Decision.ESCALATE_COPILOT,
                reason=(
                    f"fork PR: head={head_repo_full_name} base={base_repo_full_name}; "
                    "installation token cannot push to a fork"
                ),
            )

    if error_output and len(error_output) > max_error_output_chars:
        return ClassifierResult(
            decision=Decision.ESCALATE_COPILOT,
            reason=(
                f"error_output is {len(error_output)} chars "
                f"(> {max_error_output_chars}); likely a large failure"
            ),
        )

    return ClassifierResult(decision=Decision.ROUTE_FOUNDRY, reason="eligible")


def post_flight(
    *,
    files_changed: int,
    insertions: int,
    deletions: int,
    max_files_touched: int,
    max_diff_lines: int,
) -> ClassifierResult:
    """Decide whether a completed tool-loop's diff is small enough to push.

    Called after the tool loop, before commit/push.  If the diff is oversized
    we escalate to Copilot so a human/reviewer-friendly identity owns the
    larger change.
    """
    if files_changed > max_files_touched:
        return ClassifierResult(
            decision=Decision.ESCALATE_COPILOT,
            reason=(
                f"diff touches {files_changed} files (> max_files_touched={max_files_touched})"
            ),
        )
    total_lines = insertions + deletions
    if total_lines > max_diff_lines:
        return ClassifierResult(
            decision=Decision.ESCALATE_COPILOT,
            reason=(f"diff is {total_lines} lines (> max_diff_lines={max_diff_lines})"),
        )
    return ClassifierResult(decision=Decision.ROUTE_FOUNDRY, reason="within_budget")


# ──────────────────────────────────────────────────────────────────────────
# Hybrid floor/ceiling decision API
# ──────────────────────────────────────────────────────────────────────────


class SizeVerdict(BaseModel):
    """LLM-emitted verdict in the borderline zone.

    Schema kept tight: closed enum + confidence + free-text reason. The
    consensus engine compares verdicts on ``decision`` for AlwaysTwoModels
    if a site ever swaps strategies.
    """

    decision: Decision
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str = Field(max_length=300)


_SIZE_SYSTEM_PROMPT = """\
You are caretaker's size_classifier. Given a Foundry task summary,
decide whether the diff/error is small enough to route to the
Foundry executor (ROUTE_FOUNDRY) or should escalate to Copilot
(ESCALATE_COPILOT).

Rules:
- Mechanical refactors (rename, lint fix, type tightening) — ROUTE_FOUNDRY
  even at higher line counts.
- Genuinely complex logic changes, multi-package refactors, or anything
  touching auth/migrations/public APIs — ESCALATE_COPILOT.
- ``confidence`` is your self-assessed probability the verdict is correct.
- ``reason`` must be a single line no longer than 300 characters.
"""


async def decide_pre(
    *,
    task_type: str,
    allowed_task_types: list[str],
    head_repo_full_name: str | None,
    base_repo_full_name: str | None,
    route_same_repo_only: bool,
    error_output: str,
    max_error_output_chars: int = 16_000,
    borderline_low_error_chars: int = 4_000,
    borderline_high_error_chars: int = 12_000,
) -> ClassifierResult:
    """Async pre-flight gate with deterministic floor/ceiling.

    Below ``borderline_low_error_chars`` → always ROUTE_FOUNDRY.
    Above ``borderline_high_error_chars`` (or any other deterministic
    rejection like task-type mismatch / fork PR) → ESCALATE_COPILOT.
    Between → consult the consensus engine (when active).
    """
    # Hard rejections — task type, fork PR — short-circuit.
    legacy = pre_flight(
        task_type=task_type,
        allowed_task_types=allowed_task_types,
        head_repo_full_name=head_repo_full_name,
        base_repo_full_name=base_repo_full_name,
        route_same_repo_only=route_same_repo_only,
        error_output=error_output,
        max_error_output_chars=max_error_output_chars,
    )
    if legacy.decision != Decision.ROUTE_FOUNDRY:
        # Above ceiling or hard reject — return as-is.
        return legacy

    error_chars = len(error_output or "")
    if error_chars < borderline_low_error_chars:
        return legacy  # well under low band — fast path stays

    if error_chars > borderline_high_error_chars:
        return ClassifierResult(
            decision=Decision.ESCALATE_COPILOT,
            reason=(
                f"error_output is {error_chars} chars (> borderline high "
                f"{borderline_high_error_chars}); escalating without engine consult"
            ),
        )

    # Borderline — consult engine if active.
    return await _engine_consult_or_fallback(
        site="size_classifier",
        prompt=(
            f"Foundry pre-flight summary:\n"
            f"- task_type: {task_type}\n"
            f"- error_output_chars: {error_chars}\n"
            f"- error_output_excerpt:\n{(error_output or '')[:1000]}\n"
        ),
        fallback=legacy,
    )


async def decide_post(
    *,
    files_changed: int,
    insertions: int,
    deletions: int,
    max_files_touched: int,
    max_diff_lines: int,
    borderline_low_files: int = 3,
    borderline_high_files: int = 10,
    borderline_low_lines: int = 100,
    borderline_high_lines: int = 300,
) -> ClassifierResult:
    """Async post-flight gate with deterministic floor/ceiling.

    Both files-changed and total-lines have their own borderline bands.
    A diff is "borderline" if **either** dimension is in its band — gives
    the engine a chance to weigh "lots of small files" vs "few large
    files" cases.
    """
    legacy = post_flight(
        files_changed=files_changed,
        insertions=insertions,
        deletions=deletions,
        max_files_touched=max_files_touched,
        max_diff_lines=max_diff_lines,
    )
    if legacy.decision == Decision.ESCALATE_COPILOT:
        # Above the hard ceiling — never consult the engine; it can't
        # rescue a 25-file diff.
        return legacy

    total_lines = insertions + deletions
    files_borderline = borderline_low_files <= files_changed <= borderline_high_files
    lines_borderline = borderline_low_lines <= total_lines <= borderline_high_lines

    if files_changed < borderline_low_files and total_lines < borderline_low_lines:
        # Well under both floors — fast path.
        return legacy

    if files_changed > borderline_high_files or total_lines > borderline_high_lines:
        return ClassifierResult(
            decision=Decision.ESCALATE_COPILOT,
            reason=(
                f"borderline ceiling exceeded: files={files_changed} "
                f"lines={total_lines}; escalating without engine consult"
            ),
        )

    if not (files_borderline or lines_borderline):
        return legacy  # in mixed gray-zones we trust the legacy gate

    return await _engine_consult_or_fallback(
        site="size_classifier",
        prompt=(
            f"Foundry post-flight diff summary:\n"
            f"- files_changed: {files_changed}\n"
            f"- insertions: {insertions}\n"
            f"- deletions: {deletions}\n"
            f"- total_lines: {total_lines}\n"
        ),
        fallback=legacy,
    )


async def _engine_consult_or_fallback(
    *,
    site: str,
    prompt: str,
    fallback: ClassifierResult,
) -> ClassifierResult:
    """Call the consensus engine when active; on failure return ``fallback``."""
    from caretaker.consensus import active as consensus_active
    from caretaker.consensus.result import ConsensusUnavailable

    engine = consensus_active.get_active_engine()
    if engine is None or not engine.has_site(site):
        return fallback

    try:
        result = await engine.decide(
            site_name=site,
            schema=SizeVerdict,
            system_prompt=_SIZE_SYSTEM_PROMPT,
            user_prompt=prompt,
            feature="size_classifier",
        )
    except ConsensusUnavailable:
        return fallback

    return ClassifierResult(
        decision=result.verdict.decision,
        reason=f"engine: {result.verdict.reason}",
    )
