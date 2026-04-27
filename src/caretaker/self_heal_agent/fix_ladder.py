"""Deterministic-first fix ladder — Wave A3 of the R&D master plan.

Before the self-heal agent escalates a CI failure to the LLM path,
the ladder runs an ordered set of cheap, signature-gated rungs
against a working-tree sandbox. The pattern is the "BitsAI-Fix /
Factory.ai / KubeIntellect" deterministic-first approach: most real
CI failures are formatter churn, lint autofix, stale mypy stubs,
dependency resolution drift, or a flaky-last-failure test retry —
none of which require a model call.

The ladder produces one of five outcomes:

* ``fixed`` — a rung produced a non-empty diff AND the error signature
  no longer reproduces. Caller opens a PR via :class:`GitHubClient`.
* ``partial`` — some rungs produced diffs but the signature is still
  present. Caller opens a PR AND surfaces the remaining work via the
  escalation prompt.
* ``escalated`` — the ladder made no progress. The escalation prompt
  carries the error signature, the rungs tried, the top-5 past
  ``:Incident`` nodes from the memory retriever, and a pointer to
  ``CLAUDE.md`` so the LLM sees what the deterministic pass already
  ruled out.
* ``no_op`` — no rung's signature gate matched the incident. Treated
  like today: pass through to the legacy escalation path unchanged.
* ``error`` — the sandbox itself failed (e.g. git not on PATH, working
  tree missing). Caller should fall back to the legacy path.

Config is gated behind :class:`~caretaker.config.FixLadderConfig`.
Defaults to ``enabled=False`` — the ladder opens PRs autonomously,
so we ship off-by-default and let operators promote per-repo.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field

from caretaker.self_heal_agent.sandbox import FixLadderSandbox, RungExecution, git_diff

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from caretaker.memory.retriever import MemoryRetriever

logger = logging.getLogger(__name__)


# ── Incident + ladder types ──────────────────────────────────────────────


@dataclass
class Incident:
    """Summary of the failure the ladder is trying to repair.

    The self-heal agent builds this from the classified job log: the
    ``error_signature`` is the 12-char hash produced by
    :func:`caretaker.self_heal_agent.agent._sig`, ``kind`` is the
    legacy :class:`FailureKind`, ``log_tail`` is the last few KiB of
    job output (used for signature-regex matching and for seeding the
    escalation prompt), and ``repo_slug`` / ``run_id`` feed the graph
    write path.
    """

    error_signature: str
    kind: str
    log_tail: str
    repo_slug: str
    job_name: str
    run_id: int | None = None


class FixLadderRung(BaseModel):
    """One rung in the deterministic ladder.

    ``only_if_signature_matches`` is an optional list of regex
    patterns (tested as an ``re.search`` alternation) against the
    incident's ``error_sig`` *and* ``log_tail``. When ``None`` the
    rung runs unconditionally — rarely useful in practice; every
    shipping rung in the default ladder is gated.

    ``timeout_seconds`` bounds wall-clock for the rung subprocess;
    values >30s defeat the purpose of the deterministic ladder (see
    module docstring) so operators supplying their own ladder should
    keep rungs short. The default sandbox still accepts up to the
    ceiling, it just won't block the dispatch event loop waiting.
    """

    name: str
    command: list[str]
    only_if_signature_matches: list[str] | None = None
    timeout_seconds: int = 120


class RungExecutionRecord(BaseModel):
    """Pydantic-serialisable view of a :class:`RungExecution`.

    Kept separate from the dataclass so callers that persist the
    result (graph writer, escalation prompt) don't need to reach for
    ``dataclasses.asdict`` and the model field list stays explicit
    about what's surfaced to operators.
    """

    name: str
    command: list[str]
    exit_code: int
    stdout_tail: str
    stderr_tail: str
    duration_seconds: float
    timed_out: bool = False
    produced_diff: bool = False


class FixLadderResult(BaseModel):
    """Terminal verdict from :func:`run_fix_ladder`."""

    outcome: Literal["fixed", "no_op", "partial", "escalated", "error"]
    rungs_run: list[RungExecutionRecord] = Field(default_factory=list)
    patch: str | None = None
    commit_message: str | None = None
    escalation_prompt: str | None = None
    # Used by the agent to stamp ``:Incident`` node properties — the
    # rung that produced the winning diff (``fixed`` outcome only),
    # or ``None`` when no single rung is decisive.
    winning_rung: str | None = None


# ── Default ladder ───────────────────────────────────────────────────────
#
# Signatures live inline so the ladder definition reads top-to-bottom.
# Operators can replace the default via :func:`run_fix_ladder`'s
# ``rungs=`` argument without patching this module.


DEFAULT_RUNGS: list[FixLadderRung] = [
    FixLadderRung(
        name="ruff-format",
        command=["ruff", "format", "."],
        only_if_signature_matches=[
            r"fix end of files",
            r"[Ww]ould reformat",
            r"trailing whitespace",
            r"missing newline",
        ],
        timeout_seconds=30,
    ),
    FixLadderRung(
        name="ruff-check-fix",
        # ``--fix-only`` keeps the rung idempotent (no exit-1 on
        # remaining lints); ``--unsafe-fixes`` is deliberately off so
        # the rung only applies the safe autofix subset. Operators
        # who want aggressive fixes should layer their own rung.
        command=["ruff", "check", "--fix-only", "."],
        only_if_signature_matches=[
            r"\bE\d{3}\b",
            r"\bF\d{3}\b",
            r"\bW\d{3}\b",
        ],
        timeout_seconds=30,
    ),
    FixLadderRung(
        name="mypy-install-types",
        command=["mypy", "--install-types", "--non-interactive", "src"],
        only_if_signature_matches=[
            r"[Ll]ibrary stubs not installed",
            r"missing stubs",
            r"stubs not installed for",
        ],
        timeout_seconds=120,
    ),
    FixLadderRung(
        name="pip-compile-upgrade",
        # Inline wrapper: rung-specific since it has to parse the
        # failing package name out of the log. See
        # :func:`_extract_resolution_package` and
        # :func:`_pip_compile_command` below — the command list is
        # materialised at dispatch time, not here.
        command=["pip-compile", "--upgrade"],
        only_if_signature_matches=[
            r"could not find a version that satisfies",
            r"resolution-impossible",
            r"ResolutionImpossible",
        ],
        timeout_seconds=120,
    ),
    FixLadderRung(
        name="pytest-lastfail",
        # ``-x`` stops at the first still-failing test so the rung
        # stays under 30s on a repo of any size; ``--lf`` picks up
        # the cache from the failing CI run. The cap of N=3 failing
        # tests is enforced at signature-gate time below.
        command=["pytest", "--lf", "-x", "-q"],
        only_if_signature_matches=[
            r"FAILED tests/.*::",
        ],
        timeout_seconds=120,
    ),
]


# ── Signature matching ───────────────────────────────────────────────────


def _rung_matches(rung: FixLadderRung, incident: Incident) -> bool:
    """Return True when the rung's signature gate fires for ``incident``.

    Gates are regex alternations tested against both the raw
    ``error_signature`` and the ``log_tail`` — the former lets tests
    drive the gate deterministically without crafting a full log,
    the latter is what fires in production.
    """
    patterns = rung.only_if_signature_matches
    if not patterns:
        return True
    haystack = f"{incident.error_signature}\n{incident.log_tail}"
    for pattern in patterns:
        try:
            if re.search(pattern, haystack):
                return True
        except re.error:
            logger.warning("fix_ladder: invalid regex in rung %s: %s", rung.name, pattern)
            continue
    return False


def _pytest_lastfail_failing_count(log_tail: str, *, cap: int = 3) -> int:
    """Count ``FAILED tests/...`` lines in the tail; refuse above ``cap``.

    The ladder only reaches for ``pytest --lf -x`` when the failing
    set is small. A 30-test failure is almost always an environmental
    problem (wrong Python, missing env var) and retrying will burn
    CI minutes without fixing anything. Callers use the return value
    ``> cap`` to disable the rung for that incident.
    """
    return sum(1 for _ in re.finditer(r"FAILED tests/.*::", log_tail))


_PIP_COMPILE_PKG_PATTERN = re.compile(
    r"[Nn]o matching distribution found for\s+([A-Za-z0-9_.\-][A-Za-z0-9_.=<>!\-]*)"
    r"|could not find a version that satisfies the requirement\s+"
    r"([A-Za-z0-9_.\-][A-Za-z0-9_.=<>!\-]*)",
    re.IGNORECASE,
)


def _extract_resolution_package(log_tail: str) -> str | None:
    """Pull a failing package name out of a pip / pip-compile log.

    Returns ``None`` when no package can be identified — the caller
    then runs the fallback ``pip-compile --upgrade`` command (whole
    lockfile) rather than targeting a single dep.
    """
    match = _PIP_COMPILE_PKG_PATTERN.search(log_tail)
    if not match:
        return None
    return match.group(1) or match.group(2)


def _materialise_command(rung: FixLadderRung, incident: Incident) -> list[str]:
    """Rewrite the rung command for incident-specific arguments.

    ``pip-compile-upgrade`` needs the failing package name inlined;
    everything else runs the command verbatim.
    """
    if rung.name != "pip-compile-upgrade":
        return list(rung.command)
    package = _extract_resolution_package(incident.log_tail)
    if package:
        return ["pip-compile", "--upgrade-package", package]
    return list(rung.command)


# ── Signature reproduction check ─────────────────────────────────────────
#
# After a rung runs we need to know whether the original error
# signature still appears. The cheap check: if a key line from the
# log tail is still present in either stream of a subsequent
# hypothetical run, the bug is unfixed. We don't re-run CI — the
# deterministic ladder's whole point is to avoid that. Instead we
# treat a non-empty diff *plus* the rung exiting 0 as strong enough
# evidence that the signature no longer reproduces from this rung's
# angle; the CI retry after PR open is the real confirmation.


def _rung_made_progress(execution: RungExecution, diff_before: str, diff_after: str) -> bool:
    """Return True when the rung produced a new diff.

    "Progress" is defined as: after the rung, ``git diff`` emits more
    content than before. A zero-byte diff means the rung ran but
    didn't mutate any file — e.g. ``ruff format`` against an already
    clean tree. We don't count the rung's own non-zero exit code as
    progress because rungs like ``pytest --lf`` can "succeed" (exit
    0) on a flake without having changed anything.
    """
    return bool(diff_after and diff_after != diff_before)


# ── Runner ───────────────────────────────────────────────────────────────


DiffCallable = "Callable[[Path | str], str]"


async def run_fix_ladder(
    incident: Incident,
    *,
    sandbox: FixLadderSandbox,
    rungs: list[FixLadderRung] | None = None,
    memory_retriever: MemoryRetriever | None = None,
    agent_name: str = "self_heal_agent",
    max_rungs: int = 6,
    diff_fn: Callable[[Path | str], str] | None = None,
    metrics_sink: Callable[[str, str], None] | None = None,
    escalation_metrics_sink: Callable[[str, str], None] | None = None,
) -> FixLadderResult:
    """Run the deterministic fix ladder against ``sandbox``.

    The caller is responsible for having staged the working tree
    (HEAD matches the incident's SHA) and for wiring
    ``memory_retriever`` when available. When no retriever is passed
    the escalation prompt still contains the rung list and error
    signature; only the "top-5 past incidents" block is omitted.

    ``metrics_sink`` / ``escalation_metrics_sink`` are optional
    injection points so tests can assert emissions without touching
    the Prometheus registry; production wiring points at
    :mod:`caretaker.observability.metrics`.
    """
    ladder = list(rungs if rungs is not None else DEFAULT_RUNGS)
    diff = diff_fn if diff_fn is not None else git_diff

    # First pass: filter rungs whose signature gate fires.
    candidate_rungs: list[FixLadderRung] = []
    for rung in ladder:
        if not _rung_matches(rung, incident):
            continue
        if rung.name == "pytest-lastfail":
            failing = _pytest_lastfail_failing_count(incident.log_tail)
            if failing > 3:
                logger.info(
                    "fix_ladder: skipping pytest-lastfail — %d failing tests exceeds cap",
                    failing,
                )
                continue
        candidate_rungs.append(rung)

    if not candidate_rungs:
        logger.info("fix_ladder: no rung matched signature for %s", incident.error_signature)
        return FixLadderResult(outcome="no_op")

    # Two passes maximum: step 4 of the brief says "loop once more"
    # when partial progress is detected on the first sweep.
    records: list[RungExecutionRecord] = []
    any_progress = False
    any_exec_ok = False
    executed = 0
    cap = max(1, max_rungs)

    for sweep in range(2):
        swept_progress = False
        for rung in candidate_rungs:
            if executed >= cap:
                logger.info(
                    "fix_ladder: max_rungs_per_incident=%d reached, stopping",
                    cap,
                )
                break
            command = _materialise_command(rung, incident)
            diff_before = _safe_diff(diff, sandbox.working_tree)
            try:
                execution = await sandbox.run(
                    rung.name, command, timeout_seconds=rung.timeout_seconds
                )
            except Exception as exc:  # noqa: BLE001 - sandbox errors bubble as ``error`` outcome
                logger.warning("fix_ladder: rung %s raised: %s", rung.name, exc)
                if metrics_sink is not None:
                    metrics_sink(rung.name, "error")
                return FixLadderResult(
                    outcome="error",
                    rungs_run=records,
                )
            diff_after = _safe_diff(diff, sandbox.working_tree)
            progressed = _rung_made_progress(execution, diff_before, diff_after)
            executed += 1
            any_exec_ok = any_exec_ok or execution.exit_code == 0
            record = RungExecutionRecord(
                name=execution.name,
                command=execution.command,
                exit_code=execution.exit_code,
                stdout_tail=execution.stdout_tail,
                stderr_tail=execution.stderr_tail,
                duration_seconds=execution.duration_seconds,
                timed_out=execution.timed_out,
                produced_diff=progressed,
            )
            records.append(record)
            if metrics_sink is not None:
                metrics_sink(rung.name, "progress" if progressed else "no_progress")
            if progressed:
                any_progress = True
                swept_progress = True

        if swept_progress and sweep == 0:
            # Second sweep: rungs that didn't match on the first
            # pass (e.g. a lint rule revealed by a format pass) get
            # another chance. The ``swept_progress`` flag prevents
            # an infinite loop when the tree stabilises.
            continue
        break

    final_diff = _safe_diff(diff, sandbox.working_tree)
    if any_progress and final_diff:
        winning = next(
            (r.name for r in records if r.produced_diff),
            None,
        )
        commit_message = _compose_commit_message(records, incident)
        # Conservative heuristic: if the rung that exited 0 AND left
        # a non-empty diff is the only one we needed, treat as
        # ``fixed``. Otherwise mark as ``partial`` so the escalation
        # prompt still fires with "here's what the ladder did".
        outcome: Literal["fixed", "partial"]
        if _looks_fully_resolved(records):
            outcome = "fixed"
        else:
            outcome = "partial"
            if escalation_metrics_sink is not None:
                escalation_metrics_sink(incident.repo_slug, incident.error_signature)
        escalation_prompt = (
            None
            if outcome == "fixed"
            else await _build_escalation_prompt(
                incident,
                records,
                memory_retriever=memory_retriever,
                agent_name=agent_name,
            )
        )
        return FixLadderResult(
            outcome=outcome,
            rungs_run=records,
            patch=final_diff,
            commit_message=commit_message,
            escalation_prompt=escalation_prompt,
            winning_rung=winning,
        )

    # No progress — either every rung was a no-op or exits failed.
    # Build the escalation prompt regardless of whether the LLM path
    # is wired; the self-heal agent's caller decides what to do with it.
    if escalation_metrics_sink is not None:
        escalation_metrics_sink(incident.repo_slug, incident.error_signature)
    escalation_prompt = await _build_escalation_prompt(
        incident,
        records,
        memory_retriever=memory_retriever,
        agent_name=agent_name,
    )
    return FixLadderResult(
        outcome="escalated",
        rungs_run=records,
        escalation_prompt=escalation_prompt,
    )


def _safe_diff(diff_fn: Callable[[Path | str], str], working_tree: Path | str) -> str:
    """Call ``diff_fn``; swallow errors so the ladder never fails on observability."""
    try:
        return diff_fn(working_tree)
    except Exception as exc:  # noqa: BLE001 - diagnostic helper, never fatal
        logger.info("fix_ladder: diff fn raised (%s) — treating as empty", exc)
        return ""


def _looks_fully_resolved(records: list[RungExecutionRecord]) -> bool:
    """Heuristic: every rung that actually ran exited 0, at least one produced a diff.

    A zero-exit + non-empty diff across the rungs that fired is a
    strong signal the deterministic fix did its job. A non-zero exit
    on any progressing rung drops us to ``partial`` so the LLM still
    sees what was tried.
    """
    if not records:
        return False
    if not any(r.produced_diff for r in records):
        return False
    for record in records:
        if record.timed_out:
            return False
        if record.exit_code != 0 and record.produced_diff:
            return False
    return True


def _compose_commit_message(records: list[RungExecutionRecord], incident: Incident) -> str:
    """Build the commit message for a ladder-generated PR.

    Cites every rung that produced a diff (skipping pure no-op
    rungs) so operators reviewing the PR can trace the fix back to
    the exact deterministic step.
    """
    producing = [r.name for r in records if r.produced_diff]
    if not producing:
        # Should not happen when this is called — caller only
        # composes a message after detecting progress — but keep a
        # defensible fallback rather than an empty string.
        producing = [r.name for r in records] or ["fix-ladder"]
    primary = producing[0]
    subject = f"fix(auto): {primary} applied automatically"
    body_lines = [
        "",
        f"Fix-ladder rungs applied for incident sig:{incident.error_signature}.",
        "",
        "Rungs that produced changes:",
    ]
    body_lines.extend(f"- {name}" for name in producing)
    body_lines.append("")
    body_lines.append(
        "This commit was generated by the caretaker self-heal fix ladder — see "
        "`src/caretaker/self_heal_agent/fix_ladder.py` for the rung definitions."
    )
    return subject + "\n" + "\n".join(body_lines)


async def _build_escalation_prompt(
    incident: Incident,
    records: list[RungExecutionRecord],
    *,
    memory_retriever: MemoryRetriever | None = None,
    agent_name: str = "self_heal_agent",
) -> str:
    """Assemble the LLM-escalation prompt once the ladder gives up.

    Contents mandated by the brief:

    * error signature
    * list of rungs tried (names only — the full stderr tails go in
      a collapsible block so the prompt stays token-efficient)
    * top-5 past ``:Incident`` nodes via
      :class:`~caretaker.memory.retriever.MemoryRetriever`, when
      wired
    * CLAUDE.md norms pointer so the LLM knows the house rules
    """
    lines: list[str] = []
    lines.append(f"## Self-heal escalation (sig:{incident.error_signature})")
    lines.append("")
    lines.append(
        "The deterministic fix ladder could not resolve this failure. "
        "Its verdicts are below — do not re-suggest a rung that already ran."
    )
    lines.append("")
    lines.append(f"**Job:** `{incident.job_name}`")
    lines.append(f"**Kind:** `{incident.kind}`")
    lines.append(f"**Error signature:** `{incident.error_signature}`")
    if records:
        lines.append("")
        lines.append("### Rungs tried")
        for record in records:
            status = "timed out" if record.timed_out else f"exit={record.exit_code}"
            diff_flag = " (produced diff)" if record.produced_diff else ""
            lines.append(f"- `{record.name}` — {status}{diff_flag}")
    else:
        lines.append("")
        lines.append("### Rungs tried")
        lines.append("- (none matched the incident signature)")

    if memory_retriever is not None:
        try:
            hits = await memory_retriever.find_relevant(
                agent_name,
                incident.log_tail or incident.error_signature,
                repo_slug=incident.repo_slug,
            )
        except Exception as exc:  # noqa: BLE001 - retrieval must never fail escalation
            logger.info("fix_ladder: memory retriever raised: %s", exc)
            hits = []
        if hits:
            lines.append("")
            # Cap at 5 — the brief says "top-5 past :Incident nodes".
            # Retriever default is max_hits=3; honour whichever the
            # caller configured up to 5.
            top = hits[:5]
            lines.append(memory_retriever.format_for_prompt(top).rstrip())
    lines.append("")
    lines.append("### Norms")
    lines.append(
        "Follow `CLAUDE.md` in the repo root (if present) for lint / "
        "typing / test rules before proposing a patch."
    )
    lines.append("")
    if records:
        lines.append("<details><summary>Rung stderr tails</summary>")
        lines.append("")
        for record in records:
            if not record.stderr_tail:
                continue
            lines.append(f"**{record.name}**")
            lines.append("```")
            lines.append(record.stderr_tail.rstrip())
            lines.append("```")
            lines.append("")
        lines.append("</details>")
    return "\n".join(lines).rstrip() + "\n"


__all__ = [
    "DEFAULT_RUNGS",
    "FixLadderResult",
    "FixLadderRung",
    "Incident",
    "RungExecutionRecord",
    "run_fix_ladder",
]
