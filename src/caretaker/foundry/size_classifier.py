"""Pre- and post-flight eligibility gates for the Foundry executor.

The classifier is intentionally simple: cheap heuristics on metadata, no
LLM call.  Its purpose is to keep Foundry focused on XS/S/SM tasks and
escalate anything larger to Copilot before wasting tokens or — post-flight —
before pushing an overly large diff.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


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
