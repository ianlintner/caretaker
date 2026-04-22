"""CI failure triage — classifies and creates fix instructions.

Part of the Phase 2 agentic migration (see
``docs/plans/2026-Q2-agentic-migration.md`` §3.3 and task T-A3). Shipping
two classifiers side-by-side:

* :func:`classify_failure` — the legacy keyword-regex ladder. Returns a
  coarse :class:`FailureType` from job name + output title + summary. On
  ``main`` this ladder has been dropping most real failures into
  :attr:`FailureType.UNKNOWN` (issues #412/#413/#462), which is why we
  need the LLM candidate below.
* :func:`classify_failure_llm` — the LLM candidate. Calls
  :meth:`ClaudeClient.structured_complete` with :class:`FailureTriage` as
  the schema. Returns ``None`` on provider / parsing failure so callers
  can fall through to legacy.

Both paths are exposed so :func:`triage_failure` can run them under
``@shadow_decision("ci_triage")`` — no behaviour change until the flag
flips from ``off`` to ``shadow``/``enforce``.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field

from caretaker.evolution.shadow import shadow_decision
from caretaker.guardrails import sanitize_input
from caretaker.llm.claude import StructuredCompleteError

if TYPE_CHECKING:
    from caretaker.github_client.models import CheckRun
    from caretaker.llm.claude import ClaudeClient
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


# ── LLM schema (T-A3) ────────────────────────────────────────────────────
#
# The Phase 2 schema is deliberately richer than :class:`FailureType`.
# ``is_transient`` subsumes both the current ``NON_ACTIONABLE_CONCLUSIONS``
# frozenset and the ``CIConfig.flaky_retries`` counter: once the candidate
# is authoritative (mode=enforce), callers should branch on that bool
# rather than re-deriving transience from the raw conclusion.

FailureCategory = Literal[
    "test",
    "lint",
    "build",
    "type",
    "timeout",
    "flaky",
    "backpressure",
    "infra",
    "unknown",
]


class FailureTriage(BaseModel):
    """LLM-structured CI failure triage verdict.

    Returned by :func:`classify_failure_llm` and by
    :func:`classify_failure_adapter` (which lifts the legacy regex verdict
    into the same shape so shadow-mode comparisons work).
    """

    category: FailureCategory
    confidence: float = Field(ge=0.0, le=1.0)
    is_transient: bool = Field(
        description=(
            "True when the failure should be retried without code changes "
            "(flaky test, backpressure, infra blip, timeout). False means "
            "the change needs a fix."
        ),
    )
    root_cause_hypothesis: str = Field(max_length=300)
    minimal_reproduction: str | None = Field(
        default=None,
        description="Shell one-liner that reproduces the failure, when feasible.",
    )
    suggested_fix: str = Field(max_length=500)
    files_to_touch: list[str] = Field(
        default_factory=list,
        description="Repo-relative paths the fix most likely needs to touch.",
    )


# ── Legacy → FailureCategory mapping ────────────────────────────────────
#
# Used by :func:`classify_failure_adapter` so the legacy regex verdict
# can be lifted into a :class:`FailureTriage`. Keep in sync with the
# regex ladder below.

_LEGACY_TO_CATEGORY: dict[FailureType, FailureCategory] = {
    FailureType.TEST_FAILURE: "test",
    FailureType.LINT_FAILURE: "lint",
    FailureType.BUILD_FAILURE: "build",
    FailureType.TYPE_ERROR: "type",
    FailureType.TIMEOUT: "timeout",
    FailureType.BACKLOG: "backpressure",
    FailureType.UNKNOWN: "unknown",
}


# Legacy regex categories that denote transient / non-fix failures. Used
# both by the legacy fallback classifier and by the LLM fallback adapter
# so ``is_transient`` stays meaningful in shadow mode.
_LEGACY_TRANSIENT = {FailureType.BACKLOG, FailureType.TIMEOUT}


# Default per-category instructions for the legacy path. Kept here
# because :func:`build_fix_instructions` still references them in
# :func:`triage_failure`.


@dataclass
class TriageResult:
    failure_type: FailureType
    job_name: str
    error_summary: str
    instructions: str
    raw_output: str
    # Populated only when the ``ci_triage`` domain runs in ``enforce`` mode
    # and the LLM candidate succeeds. In ``off`` / ``shadow`` modes this
    # remains ``None`` so downstream behaviour is byte-identical.
    #
    # Callers that want to branch on the rich verdict should fall back to
    # the legacy fields (``failure_type``, ``error_summary``) when this
    # attribute is absent.
    triage: FailureTriage | None = None


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
    """Classify a CI failure by job name and output (legacy regex ladder).

    Retained so shadow-mode still has something to compare against. On
    ``main`` this consistently returns :attr:`FailureType.UNKNOWN` for
    anything that isn't a classic ``lint``/``test``/``build`` job name;
    that's the gap T-A3 closes with :func:`classify_failure_llm`.
    """
    text = f"{check_run.name} {check_run.output_title or ''} {check_run.output_summary or ''}"

    if check_run.conclusion and check_run.conclusion.value == "timed_out":
        return FailureType.TIMEOUT

    for failure_type, patterns in _PATTERNS:
        for pattern in patterns:
            if re.search(pattern, text):
                return failure_type

    return FailureType.UNKNOWN


def classify_failure_adapter(check_run: CheckRun) -> FailureTriage:
    """Lift the legacy :func:`classify_failure` verdict into a :class:`FailureTriage`.

    Used as the shadow-mode *legacy* branch (and as the ``enforce``
    safety-net when the LLM candidate errors). Missing LLM-only fields
    get templated placeholders so the pydantic model still validates.
    """
    failure_type = classify_failure(check_run)
    category = _LEGACY_TO_CATEGORY.get(failure_type, "unknown")
    is_transient = failure_type in _LEGACY_TRANSIENT
    # Deliberately not guessing confidence from the regex ladder —
    # anything higher than 0.5 would be dishonest when the ladder's
    # well-known failure mode is "match lint by substring".
    confidence = 0.5 if failure_type is not FailureType.UNKNOWN else 0.1
    root_cause = f"Legacy heuristic: matched pattern {failure_type.value}"
    suggested_fix = "Follow the category-specific instructions produced by build_fix_instructions."
    return FailureTriage(
        category=category,
        confidence=confidence,
        is_transient=is_transient,
        root_cause_hypothesis=root_cause,
        minimal_reproduction=None,
        suggested_fix=suggested_fix,
        files_to_touch=[],
    )


# ── LLM candidate ────────────────────────────────────────────────────────

# Approximate char budget for the log tail. Matches the 8 KiB cap used by
# the existing ``analyze_ci_logs`` helper so prompt cache keys stay stable
# across the two paths.
_LOG_TAIL_CHAR_BUDGET = 8000


def _extract_log_tail(check_run: CheckRun, log_tail: str | None) -> str:
    """Return the last ~200 lines of log text for the LLM prompt.

    Callers hand in ``log_tail`` directly when they have the full job
    log; when they don't, we fall back to the ``output_summary`` /
    ``output_title`` that GitHub returns with the ``CheckRun``. Either
    way, the returned string is capped at :data:`_LOG_TAIL_CHAR_BUDGET`
    characters to keep the prompt cache-friendly.
    """
    text = log_tail
    if not text:
        text = check_run.output_summary or check_run.output_title or ""
    if not text:
        return ""
    # Keep the last ~200 lines — the failure is almost always at the
    # end of the log.
    lines = text.splitlines()
    if len(lines) > 200:
        lines = lines[-200:]
    tail = "\n".join(lines)
    if len(tail) > _LOG_TAIL_CHAR_BUDGET:
        # Preserve the tail (more diagnostic value than the head).
        tail = tail[-_LOG_TAIL_CHAR_BUDGET:]
    return tail


def _build_ci_triage_prompt(
    check_run: CheckRun,
    log_tail: str,
    *,
    language_hint: str | None = None,
    framework_hint: str | None = None,
) -> tuple[str, str]:
    """Build the (system, user) prompt pair for :func:`classify_failure_llm`.

    Keeps the stable prefix (instructions, language/framework hints) in
    the *system* slot so Anthropic prompt caching can cover it across
    invocations; the variable log tail goes into the user slot at the
    end, which is the shape cache-aware providers optimise for.
    """
    conclusion_value = check_run.conclusion.value if check_run.conclusion is not None else "unknown"
    duration_s: float | None = None
    if check_run.started_at and check_run.completed_at:
        duration_s = (check_run.completed_at - check_run.started_at).total_seconds()
    duration_str = f"{duration_s:.1f}s" if duration_s is not None else "n/a"

    hints: list[str] = []
    if language_hint:
        hints.append(f"Language: {language_hint}")
    if framework_hint:
        hints.append(f"Framework: {framework_hint}")

    system = (
        "You are a CI failure triager. Categorise a single CI check run "
        "and suggest a concrete fix. Focus on the failure, not the PR. "
        "Do not speculate about the PR title or author. If the log is "
        "empty or truncated, say so in root_cause_hypothesis and set "
        "confidence accordingly.\n\n"
        "Category definitions:\n"
        "  test — unit/integration test assertion or collection failure\n"
        "  lint — style or static-analysis gate (ruff, eslint, prettier)\n"
        "  build — compile/package/bundle failure\n"
        "  type — static type checker failure (mypy, pyright, tsc --noEmit)\n"
        "  timeout — the job hit its wall-clock or test timeout\n"
        "  flaky — the failure is intermittent; rerun likely succeeds\n"
        "  backpressure — queue-guard / too-many-runs rejection\n"
        "  infra — runner / network / credentials / external outage\n"
        "  unknown — none of the above fit\n\n"
        "Set is_transient=true only for flaky/backpressure/infra and "
        "timeouts that look environmental. Prefer is_transient=false "
        "when the log names a specific assertion, missing import, or "
        "type error."
    )
    if hints:
        system = system + "\n\nRepo context: " + "; ".join(hints)

    user = (
        f"Check run: {check_run.name}\n"
        f"Conclusion: {conclusion_value}\n"
        f"Duration: {duration_str}\n\n"
        "Log tail (most recent lines last):\n"
        f"```\n{log_tail}\n```"
    )
    return system, user


async def classify_failure_llm(
    check_run: CheckRun,
    log_tail: str | None,
    *,
    claude: ClaudeClient,
    language_hint: str | None = None,
    framework_hint: str | None = None,
) -> FailureTriage | None:
    """Ask the LLM to classify a CI failure into a :class:`FailureTriage`.

    Returns ``None`` on :class:`StructuredCompleteError` (provider
    unavailable, bad JSON, schema validation failure) so shadow-mode
    comparisons can fall through to the legacy adapter without disrupting
    the hot path.
    """
    tail = _extract_log_tail(check_run, log_tail)
    # Guardrail (Agentic Design Patterns Ch. 18): scrub the log tail for
    # prompt-injection sigils, zero-width chars, and caretaker-marker
    # echoes before it reaches the LLM prompt. Source=``ci_log`` takes
    # the tail on over-budget, matching the diagnostic value of the
    # end-of-log content.
    sanitized_tail = sanitize_input("ci_log", tail)
    tail = sanitized_tail.content
    system, prompt = _build_ci_triage_prompt(
        check_run,
        tail,
        language_hint=language_hint,
        framework_hint=framework_hint,
    )
    try:
        return await claude.structured_complete(
            prompt,
            schema=FailureTriage,
            feature="ci_triage",
            system=system,
            max_tokens=800,
        )
    except StructuredCompleteError as exc:
        logger.info(
            "ci_triage: LLM candidate failed validation (%s) — falling through to legacy",
            exc,
        )
        return None


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


# ── Shadow-wrapped dispatch ──────────────────────────────────────────────
#
# The decorator runs both paths under ``agentic.ci_triage.mode=shadow``
# and only the candidate under ``enforce``. The custom ``compare``
# predicate ignores the noisy free-text fields so small rewording in
# ``root_cause_hypothesis`` doesn't spam the disagreement counter.


def _triage_compare(a: Any, b: Any) -> bool:
    """Shadow-mode comparator: match on category + transience only."""
    if a is None or b is None:
        return a is b
    try:
        return bool(a.category == b.category and a.is_transient == b.is_transient)
    except AttributeError:
        return False


@shadow_decision("ci_triage", compare=_triage_compare)
async def _dispatch_triage(
    *,
    legacy: Any,
    candidate: Any,
    context: Any = None,
) -> FailureTriage:
    """Thin shim the decorator drives. The actual work lives in
    ``legacy`` / ``candidate`` callables supplied by :func:`triage_failure`.
    """
    raise AssertionError(
        "@shadow_decision wrapper should dispatch via legacy/candidate"
    )  # pragma: no cover


async def triage_failure(
    check_run: CheckRun,
    llm_router: LLMRouter | None = None,
    *,
    repo_slug: str | None = None,
) -> TriageResult:
    """Triage a CI failure — classify and generate fix instructions.

    Runs under the ``agentic.ci_triage`` flag via :func:`shadow_decision`:

    * ``off`` (default) — legacy regex ladder only; byte-identical to the
      pre-T-A3 behaviour.
    * ``shadow`` — both paths run, legacy verdict is returned, the LLM
      verdict is recorded next to it so operators can inspect the
      disagreement rate before flipping authority.
    * ``enforce`` — the LLM verdict wins; legacy is the fall-through
      safety net when the candidate errors or returns ``None``.
    """
    raw_output = check_run.output_summary or check_run.output_title or ""

    claude: ClaudeClient | None = None
    if llm_router is not None and llm_router.claude_available:
        claude = llm_router.claude

    async def _legacy() -> FailureTriage:
        return classify_failure_adapter(check_run)

    async def _candidate() -> FailureTriage | None:
        # ``enforce`` needs a real candidate — if the LLM isn't wired up
        # we return None so the decorator falls through to legacy.
        if claude is None or not claude.available:
            return None
        return await classify_failure_llm(check_run, raw_output, claude=claude)

    verdict = await _dispatch_triage(
        legacy=_legacy,
        candidate=_candidate,
        context={"repo_slug": repo_slug or "", "job_name": check_run.name},
    )

    # Translate the FailureTriage verdict back into the legacy dataclass
    # shape PR agent / copilot bridge consume. While ``mode`` stays ``off``
    # or ``shadow``, ``verdict`` came from the legacy adapter, so
    # :func:`classify_failure` on the same ``check_run`` yields the same
    # ``FailureType`` — the conversion is loss-free. Only in ``enforce``
    # mode might the LLM verdict disagree with the legacy regex, and we
    # document below how the new category rows map back.
    failure_type = _category_to_failure_type(verdict.category)

    error_summary = raw_output[:500]
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
        triage=verdict,
    )


def _category_to_failure_type(category: FailureCategory) -> FailureType:
    """Map a :data:`FailureCategory` back to the legacy :class:`FailureType`.

    The mapping is 1:1 where the legacy enum has a row, and falls back
    to :attr:`FailureType.UNKNOWN` for the LLM-only categories (``flaky``,
    ``infra``). Callers that care about transience should read
    :attr:`FailureTriage.is_transient` rather than re-deriving it from the
    :class:`FailureType` — see T-A3 notes in the module docstring.
    """
    return {
        "test": FailureType.TEST_FAILURE,
        "lint": FailureType.LINT_FAILURE,
        "build": FailureType.BUILD_FAILURE,
        "type": FailureType.TYPE_ERROR,
        "timeout": FailureType.TIMEOUT,
        "backpressure": FailureType.BACKLOG,
        # ``flaky`` / ``infra`` / ``unknown`` have no legacy row. Route
        # them to UNKNOWN so the PR Agent's existing UNKNOWN-with-empty-
        # logs guard still fires, and so the Copilot bridge falls back
        # to the generic CI_FAILURE task type.
        "flaky": FailureType.UNKNOWN,
        "infra": FailureType.UNKNOWN,
        "unknown": FailureType.UNKNOWN,
    }[category]
