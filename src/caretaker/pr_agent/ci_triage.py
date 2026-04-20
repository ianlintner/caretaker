"""CI failure triage — classifies and creates fix instructions."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from caretaker.github_client.models import CheckRun
    from caretaker.llm.router import LLMRouter

logger = logging.getLogger(__name__)


class FailureType(StrEnum):
    BACKLOG = "BACKLOG"
    TEST_FAILURE = "TEST_FAILURE"
    LINT_FAILURE = "LINT_FAILURE"
    BUILD_FAILURE = "BUILD_FAILURE"
    TYPE_ERROR = "TYPE_ERROR"
    TIMEOUT = "TIMEOUT"
    UNKNOWN = "UNKNOWN"


@dataclass
class TriageResult:
    failure_type: FailureType
    job_name: str
    error_summary: str
    instructions: str
    raw_output: str


# Patterns for classifying CI failures by job name / output
_PATTERNS: list[tuple[FailureType, list[str]]] = [
    (
        FailureType.BACKLOG,
        [
            r"(?i)queue-guard",
            r"(?i)backpressure",
            r"(?i)backlog",
            r"(?i)queue pressure",
            r"(?i)too many active pull_request ci runs",
            r"(?i)failing this run early",
        ],
    ),
    (
        FailureType.TEST_FAILURE,
        [
            r"(?i)test",
            r"(?i)jest",
            r"(?i)pytest",
            r"(?i)mocha",
            r"(?i)vitest",
            r"FAIL\s+\S+",
            r"Expected.*Received",
            r"AssertionError",
        ],
    ),
    (
        FailureType.LINT_FAILURE,
        [
            r"(?i)lint",
            r"(?i)eslint",
            r"(?i)ruff",
            r"(?i)flake8",
            r"(?i)prettier",
            r"(?i)stylelint",
        ],
    ),
    (
        FailureType.BUILD_FAILURE,
        [
            r"(?i)build",
            r"(?i)compile",
            r"(?i)tsc",
            r"error TS\d+",
            r"SyntaxError",
        ],
    ),
    (
        FailureType.TYPE_ERROR,
        [
            r"(?i)typecheck",
            r"(?i)mypy",
            r"(?i)pyright",
            r"error TS\d+",
        ],
    ),
]


# Conclusions that should NOT trigger a Copilot fix request. These come from
# CI runs that were never given a chance to fail meaningfully — workflow
# cancellation cascade, intentional skip, neutral exit. Treating them as
# failures is the upstream cause of the "[WIP] Fix CI failure (unknown)"
# self-heal PRs that get auto-closed.
NON_ACTIONABLE_CONCLUSIONS = frozenset({"cancelled", "skipped", "neutral"})


def is_actionable_conclusion(conclusion: object) -> bool:
    """Return True if a check_run conclusion warrants triage / fix-request.

    Accepts either a CheckConclusion enum, the raw string value, or None
    (in-progress / unreported) to keep call sites simple.
    """
    if conclusion is None:
        return False
    value = getattr(conclusion, "value", conclusion)
    return value not in NON_ACTIONABLE_CONCLUSIONS


def classify_failure(check_run: CheckRun) -> FailureType:
    """Classify a CI failure by job name and output."""
    text = f"{check_run.name} {check_run.output_title or ''} {check_run.output_summary or ''}"

    if check_run.conclusion and check_run.conclusion.value == "timed_out":
        return FailureType.TIMEOUT

    for failure_type, patterns in _PATTERNS:
        for pattern in patterns:
            if re.search(pattern, text):
                return failure_type

    return FailureType.UNKNOWN


def build_fix_instructions(failure_type: FailureType, check_run: CheckRun) -> str:
    """Generate instructions for Copilot to fix a CI failure."""
    base = f"The `{check_run.name}` job failed."

    instructions = {
        FailureType.BACKLOG: (
            f"{base} — the repository backlog guard tripped.\n"
            "1. Do not change application code for this failure alone\n"
            "2. Wait for CI capacity to recover or for caretaker to clean up managed PRs\n"
            "3. Re-run CI once the queue has cleared if the PR should stay open"
        ),
        FailureType.TEST_FAILURE: (
            f"{base}\n"
            "1. Read the test failure output carefully\n"
            "2. Identify which test(s) failed and why\n"
            "3. Fix the source code to make the test pass (prefer fixing code over tests)\n"
            "4. Run all tests to verify no regressions\n"
            "5. Reply with a RESULT block when done"
        ),
        FailureType.LINT_FAILURE: (
            f"{base}\n"
            "1. Read the lint errors\n"
            "2. Fix all reported lint issues\n"
            "3. Run the linter locally to verify\n"
            "4. Reply with a RESULT block when done"
        ),
        FailureType.BUILD_FAILURE: (
            f"{base}\n"
            "1. Read the build errors\n"
            "2. Fix compilation/build issues\n"
            "3. Ensure the project builds cleanly\n"
            "4. Run tests after fixing\n"
            "5. Reply with a RESULT block when done"
        ),
        FailureType.TYPE_ERROR: (
            f"{base}\n"
            "1. Read the type errors\n"
            "2. Fix type annotations and type mismatches\n"
            "3. Run the type checker to verify\n"
            "4. Reply with a RESULT block when done"
        ),
        FailureType.TIMEOUT: (
            f"{base} — the job timed out.\n"
            "1. Check if there's an infinite loop or resource leak\n"
            "2. Check if tests are hanging or waiting on external resources\n"
            "3. If the fix isn't obvious, reply with a BLOCKED block explaining what you found"
        ),
        FailureType.UNKNOWN: (
            f"{base}\n"
            "1. Read the full job output carefully\n"
            "2. Identify the root cause\n"
            "3. Fix the issue or reply with a BLOCKED block if unclear"
        ),
    }
    return instructions.get(failure_type, instructions[FailureType.UNKNOWN])


async def triage_failure(check_run: CheckRun, llm_router: LLMRouter | None = None) -> TriageResult:
    """Triage a CI failure — classify and generate fix instructions."""
    failure_type = classify_failure(check_run)
    raw_output = check_run.output_summary or check_run.output_title or ""
    error_summary = raw_output[:500]

    # If Claude is available, get enhanced analysis
    if llm_router and llm_router.feature_enabled("ci_log_analysis") and raw_output:
        try:
            analysis = await llm_router.claude.analyze_ci_logs(raw_output)
            if analysis:
                error_summary = analysis
        except Exception:
            logger.warning("Claude CI analysis failed, falling back to heuristics")

    instructions = build_fix_instructions(failure_type, check_run)

    return TriageResult(
        failure_type=failure_type,
        job_name=check_run.name,
        error_summary=error_summary,
        instructions=instructions,
        raw_output=raw_output,
    )
