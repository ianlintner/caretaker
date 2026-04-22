"""Grouped-dependabot PR bisector.

When Dependabot ships a grouped PR that bundles many package updates
together (for example ``npm_and_yarn across 4 directories, 19 updates``)
the PR can land with ``MERGEABLE UNSTABLE``: one update in the bundle
broke CI but Caretaker has no way to tell which. Merging the whole
bundle is not safe; closing it loses every safe update.

This module parses the Dependabot PR body into a structured list of
``PackageUpdate`` rows and narrows the failing bundle down to a short
``suggested_merge_plan`` that a maintainer (or a follow-up agent) can
execute. The default integration is advisory: a comment is posted on
the PR summarising the plan. The actual CI-driven branch-bisect loop
(create-branch / push / wait-for-CI / narrow) is documented as a
follow-up and gated behind
``DependencyAgentConfig.bisector.enabled``.

Scope notes
-----------
* The parser understands Dependabot's ``Bumps the <ecosystem> group
  with N updates in the <dir> directory`` preamble plus the per-row
  "Updates <name> from X to Y" statements. Multi-directory and
  single-update blocks (``Bumps the npm_and_yarn group with 1 update
  in the /embed directory: [vite]...``) are both handled.
* ``bisect_grouped_dependabot_pr`` is **async**; when no
  ``ci_probe`` is supplied it falls back to a pure planner that
  emits an advisory merge plan and reports ``outcome="inconclusive"``
  with reason ``no_probe_configured``. Callers that want the real
  bisect loop can pass a coroutine that creates a probe branch
  applying the subset, waits for CI, and returns the outcome.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from caretaker.github_client.api import GitHubClient
    from caretaker.github_client.models import PullRequest

logger = logging.getLogger(__name__)

# Marker so caretaker can recognise its own bisect comments on subsequent
# runs and avoid duplicate-posting them.
BISECTOR_COMMENT_MARKER = "<!-- caretaker:dependency-bisect -->"

Ecosystem = Literal["npm", "pip", "cargo", "gomod", "bundler", "other"]

# "Bumps the npm_and_yarn group with 10 updates in the /backend directory:"
# "Bumps the npm_and_yarn group with 1 update in the /embed directory: [vite]..."
_GROUP_HEADER_RE = re.compile(
    r"Bumps?\s+the\s+(?P<ecosystem>[A-Za-z0-9_.\-]+)\s+group\s+with\s+"
    r"(?P<count>\d+)\s+updates?\s+in\s+the\s+(?P<directory>\S+)\s+directory",
    re.IGNORECASE,
)

# Matches a Dependabot markdown table row listing one update:
# "| [express-rate-limit](https://…) | `8.2.1` | `8.3.2` |"
# Also matches rows without the link form: "| minimatch | `3.1.2` | `3.1.5` |"
_TABLE_ROW_RE = re.compile(
    r"^\|\s*"
    r"(?:\[(?P<name_linked>[^\]]+)\]\([^)]+\)|(?P<name_plain>[^|`\[\]]+?))"
    r"\s*\|\s*"
    r"`(?P<from_version>[^`]+)`"
    r"\s*\|\s*"
    r"`(?P<to_version>[^`]+)`"
    r"\s*\|\s*$",
    re.MULTILINE,
)

# Fallback per-update paragraph form:
# "Updates `express-rate-limit` from 8.2.1 to 8.3.2"
# Version chars: digits, letters, dots, dashes, plus, underscore (covers
# semver, pre-releases like 8.0.0a1, and build metadata).
_UPDATES_LINE_RE = re.compile(
    r"Updates?\s+`(?P<name>[^`]+)`\s+from\s+"
    r"(?P<from_version>[\w.\-+]+)\s+to\s+(?P<to_version>[\w.\-+]+)",
    re.IGNORECASE,
)

# "Bumps the npm_and_yarn group with 1 update in the /embed directory:
#   [vite](https://…)." — single update with package baked into header line.
# Also matches the 2-update "inline" variant dependabot sometimes uses:
# "...with 2 updates in the /web directory: [vite](...) and [vitest](...)."
_INLINE_HEADER_RE = re.compile(
    r"Bumps?\s+the\s+(?P<ecosystem>[A-Za-z0-9_.\-]+)\s+group\s+with\s+"
    r"(?P<count>\d+)\s+updates?\s+in\s+the\s+(?P<directory>\S+)\s+directory:\s+"
    r"(?P<tail>.+)$",
    re.IGNORECASE,
)

# Extracts "[name](url)" link targets used as a package list inside
# inline headers.
_LINK_NAME_RE = re.compile(r"\[([^\]]+)\]\(")

_ECOSYSTEM_NORMALISE: dict[str, Ecosystem] = {
    "npm_and_yarn": "npm",
    "npm": "npm",
    "yarn": "npm",
    "pip": "pip",
    "pip_and_poetry": "pip",
    "cargo": "cargo",
    "gomod": "gomod",
    "go_modules": "gomod",
    "bundler": "bundler",
}


def _normalise_ecosystem(raw: str) -> Ecosystem:
    return _ECOSYSTEM_NORMALISE.get(raw.lower(), "other")


class PackageUpdate(BaseModel):
    """One package update row inside a grouped Dependabot PR."""

    ecosystem: Ecosystem
    name: str
    from_version: str
    to_version: str
    directory: str = "/"

    @property
    def slug(self) -> str:
        """Short human identifier used in merge-plan strings."""
        return f"{self.name} {self.from_version}->{self.to_version}"


# Result of running a single CI probe against a subset of updates.
# Callers wire a coroutine matching this signature when they want to
# drive the real CI-backed bisect loop. The subset is the list of
# updates the probe branch should apply; the implementation is
# responsible for creating and pushing the branch, waiting for CI,
# and returning a verdict.
CIProbe = Callable[[list[PackageUpdate]], Awaitable["ProbeOutcome"]]


class ProbeOutcome(BaseModel):
    """Result of running CI on a subset of the grouped PR."""

    passed: bool
    reason: str = ""


class BisectResult(BaseModel):
    """Structured outcome of a bisect attempt on a grouped PR."""

    outcome: Literal["all_green", "guilty_identified", "inconclusive", "error"]
    guilty_updates: list[PackageUpdate] = Field(default_factory=list)
    safe_updates: list[PackageUpdate] = Field(default_factory=list)
    suggested_merge_plan: list[str] = Field(default_factory=list)
    runs_consumed: int = 0
    reason: str = ""


# ────────────────────────────────────────────────────────────────────
# Parser
# ────────────────────────────────────────────────────────────────────


def parse_grouped_pr_body(body: str) -> list[PackageUpdate]:
    """Extract a ``list[PackageUpdate]`` from a Dependabot PR body.

    The parser is permissive: it looks at the structured header blocks
    ("Bumps the <ecosystem> group with N updates in the <dir>
    directory: ...") to assign ecosystem and directory, then harvests
    package rows from the markdown tables that follow (and, as a
    fallback, from "Updates <foo> from X to Y" lines). Duplicate
    ``(name, directory)`` pairs are de-duplicated to handle Dependabot
    occasionally repeating a row in the "Updates" detail section.
    """
    if not body:
        return []

    # Pass 1: walk the header blocks and record (ecosystem, name,
    # directory) tuples. Table rows carry their own version range;
    # inline header lines list package names but defer version info
    # to the global detail section.
    pending: list[tuple[Ecosystem, str, str, str | None, str | None]] = []
    seen_keys: set[tuple[str, str]] = set()

    def _remember(
        ecosystem: Ecosystem,
        name: str,
        directory: str,
        from_version: str | None,
        to_version: str | None,
    ) -> None:
        key = (name, directory)
        if key in seen_keys:
            return
        seen_keys.add(key)
        pending.append((ecosystem, name, directory, from_version, to_version))

    current_ecosystem: Ecosystem = "other"
    current_directory = "/"
    in_header_block = False
    in_table = False
    details_started = False

    for line in body.splitlines():
        if line.startswith("Updates `") or line.startswith("Updates ["):
            # Entering the per-package detail section; header blocks
            # have all been consumed by this point.
            details_started = True
            in_header_block = False
            in_table = False

        if details_started:
            continue

        inline = _INLINE_HEADER_RE.search(line)
        if inline:
            current_ecosystem = _normalise_ecosystem(inline.group("ecosystem"))
            current_directory = inline.group("directory")
            in_header_block = True
            in_table = False
            for name in _LINK_NAME_RE.findall(inline.group("tail")):
                cleaned = name.strip()
                if cleaned:
                    _remember(current_ecosystem, cleaned, current_directory, None, None)
            continue

        header = _GROUP_HEADER_RE.search(line)
        if header:
            current_ecosystem = _normalise_ecosystem(header.group("ecosystem"))
            current_directory = header.group("directory")
            in_header_block = True
            in_table = False
            continue

        if in_header_block:
            row = _TABLE_ROW_RE.match(line)
            if row:
                in_table = True
                name = (row.group("name_linked") or row.group("name_plain") or "").strip()
                if name and name.lower() not in {"package", "---"}:
                    _remember(
                        current_ecosystem,
                        name,
                        current_directory,
                        row.group("from_version"),
                        row.group("to_version"),
                    )
                continue
            # A blank line after a table ends the header block's
            # scope so a trailing per-update paragraph doesn't get
            # mis-attributed to the last seen directory.
            if in_table and not line.strip():
                in_header_block = False
                in_table = False

    # Pass 2: collect global "Updates `x` from Y to Z" version ranges
    # for names we recorded without versions (inline-single-update
    # groups and the like). First occurrence wins.
    detail_versions: dict[str, tuple[str, str]] = {}
    for match in _UPDATES_LINE_RE.finditer(body):
        name = match.group("name").strip()
        if name in detail_versions:
            continue
        detail_versions[name] = (
            match.group("from_version"),
            match.group("to_version"),
        )

    result: list[PackageUpdate] = []
    for ecosystem, name, directory, from_v, to_v in pending:
        if from_v is None or to_v is None:
            versions = detail_versions.get(name)
            if versions is None:
                # No version info anywhere in the PR body; drop the
                # row rather than emitting a half-populated update.
                continue
            from_v, to_v = versions
        result.append(
            PackageUpdate(
                ecosystem=ecosystem,
                name=name,
                from_version=from_v,
                to_version=to_v,
                directory=directory,
            )
        )

    # Defensive fallback: no header blocks parsed but the body does
    # contain "Updates `x` from Y to Z" lines. Preserve the original
    # lenient behaviour so unusual PR shapes still produce
    # ``PackageUpdate`` rows.
    if not result and detail_versions:
        for name, (from_v, to_v) in detail_versions.items():
            result.append(
                PackageUpdate(
                    ecosystem="other",
                    name=name,
                    from_version=from_v,
                    to_version=to_v,
                    directory="/",
                )
            )

    return result


# ────────────────────────────────────────────────────────────────────
# Plan synthesis
# ────────────────────────────────────────────────────────────────────


def synthesize_merge_plan(
    *,
    safe: list[PackageUpdate],
    guilty: list[PackageUpdate],
    inconclusive: list[PackageUpdate] | None = None,
    budget_exhausted: bool = False,
) -> list[str]:
    """Turn bisect results into a human-readable merge plan.

    The plan is an ordered list of short imperative strings ready to be
    pasted into a PR comment. Examples:

    * ``"Merge: react-dom 18.3.1"``
    * ``"Hold: typescript 5.4.5 (breaks CI)"``
    * ``"Open followup issue: typescript upgrade"``
    """
    plan: list[str] = []
    for update in safe:
        plan.append(f"Merge: {update.slug} ({update.directory})")
    for update in guilty:
        plan.append(f"Hold: {update.slug} ({update.directory}) — breaks CI")
        plan.append(f"Open followup issue: {update.name} upgrade")
    if inconclusive:
        joined = ", ".join(u.slug for u in inconclusive)
        if budget_exhausted:
            plan.append(
                f"Needs human review: bisect budget exhausted with {len(inconclusive)} "
                f"candidate(s) remaining ({joined})"
            )
        else:
            plan.append(f"Needs human review: unable to narrow ({joined})")
    return plan


# ────────────────────────────────────────────────────────────────────
# Bisect loop
# ────────────────────────────────────────────────────────────────────


async def _run_bisect(
    updates: list[PackageUpdate],
    probe: CIProbe,
    *,
    max_runs: int,
) -> tuple[list[PackageUpdate], list[PackageUpdate], list[PackageUpdate], int]:
    """Classic bisect: partition the input into guilty / safe / undetermined.

    Strategy:
    1. Probe the full bundle. If it passes, everything is safe.
    2. Otherwise split in half; probe each half.
    3. If only one half fails, recurse into that half. If both fail,
       recurse into each (two independent culprits). If neither
       fails individually the failure was an interaction — record
       the whole bundle as undetermined.
    4. Stop when a subset of size 1 fails (that update is guilty) or
       the run budget is spent.
    """
    safe: list[PackageUpdate] = []
    guilty: list[PackageUpdate] = []
    undetermined: list[PackageUpdate] = []
    runs = 0

    async def _probe(subset: list[PackageUpdate]) -> bool:
        nonlocal runs
        if runs >= max_runs:
            return False
        runs += 1
        outcome = await probe(subset)
        logger.info(
            "bisect probe: subset_size=%d run=%d passed=%s reason=%s",
            len(subset),
            runs,
            outcome.passed,
            outcome.reason,
        )
        return outcome.passed

    async def _narrow(subset: list[PackageUpdate], *, known_fails: bool = True) -> None:
        """Recursively narrow ``subset`` to guilty / safe updates.

        Precondition: ``subset`` is known to fail CI when ``known_fails``
        is True. The initial call from ``_run_bisect`` always satisfies
        this (we only recurse once the full-bundle probe has failed).
        """
        if len(subset) == 0:
            return
        if runs >= max_runs:
            undetermined.extend(subset)
            return
        if len(subset) == 1:
            # A single-element failing subset is guilty by elimination;
            # no need to burn another probe.
            if known_fails:
                guilty.append(subset[0])
            else:
                undetermined.append(subset[0])
            return

        # Special-case 2-element subsets: one probe suffices to
        # identify the guilty element. We probe the left half; if it
        # fails, the right half is safe by elimination. If the left
        # passes, the right element must be guilty (we know the pair
        # fails together).
        if len(subset) == 2:
            left_single = [subset[0]]
            left_passes = await _probe(left_single)
            if left_passes:
                # Left safe, right must be the culprit.
                safe.extend(left_single)
                guilty.append(subset[1])
            else:
                # Left is guilty; right is safe by elimination.
                guilty.extend(left_single)
                safe.append(subset[1])
            return

        mid = len(subset) // 2
        left = subset[:mid]
        right = subset[mid:]

        left_passes = await _probe(left)

        if runs >= max_runs:
            # We know left's verdict but can't test right; leave right
            # undetermined and record what we do know.
            if left_passes:
                safe.extend(left)
                undetermined.extend(right)
            else:
                undetermined.extend(subset)
            return

        if left_passes:
            # Pair known to fail, left half passes → right half fails.
            safe.extend(left)
            await _narrow(right, known_fails=True)
            return

        # Left half fails. Probe right half to distinguish
        # single-culprit (right safe) from two-culprit (right fails)
        # scenarios.
        right_passes = await _probe(right)
        if right_passes:
            safe.extend(right)
            await _narrow(left, known_fails=True)
            return
        # Both halves fail independently → recurse into both.
        await _narrow(left, known_fails=True)
        await _narrow(right, known_fails=True)

    # Step 1: full-bundle probe.
    full_passes = await _probe(updates)
    if full_passes:
        safe.extend(updates)
    else:
        await _narrow(updates)

    return safe, guilty, undetermined, runs


async def bisect_grouped_dependabot_pr(
    pr: PullRequest,
    *,
    github: GitHubClient,  # noqa: ARG001 — reserved for future CI-driven bisect
    max_runs: int = 6,
    ci_probe: CIProbe | None = None,
) -> BisectResult:
    """Identify the guilty update(s) inside a grouped Dependabot PR.

    Behaviour:

    1. Parse ``pr.body`` for the grouped-update table. Fewer than 2
       updates → ``outcome="inconclusive"`` with
       ``reason="not_grouped"``.
    2. If ``ci_probe`` is None the function operates in advisory mode:
       it returns ``outcome="inconclusive"`` with
       ``reason="no_probe_configured"`` and a plan that lists every
       parsed update as a "needs human review" entry. Callers wire the
       probe in when they're ready to drive the real CI loop.
    3. Otherwise run the bisect up to ``max_runs`` probes. If the
       budget is exhausted while the guilty set is still ambiguous
       return ``outcome="inconclusive"`` with a partial plan; never
       merge on behalf of the user inside this function.

    The function never mutates the PR; it returns a ``BisectResult``
    that the caller posts as a comment (or hands to a follow-up
    agent).
    """
    updates = parse_grouped_pr_body(pr.body or "")
    if len(updates) < 2:
        return BisectResult(
            outcome="inconclusive",
            suggested_merge_plan=[],
            runs_consumed=0,
            reason="not_grouped",
        )

    if ci_probe is None:
        plan = synthesize_merge_plan(
            safe=[],
            guilty=[],
            inconclusive=updates,
            budget_exhausted=False,
        )
        return BisectResult(
            outcome="inconclusive",
            guilty_updates=[],
            safe_updates=[],
            suggested_merge_plan=plan,
            runs_consumed=0,
            reason="no_probe_configured",
        )

    try:
        safe, guilty, undetermined, runs = await _run_bisect(updates, ci_probe, max_runs=max_runs)
    except Exception as exc:  # pragma: no cover — defensive
        logger.exception("bisect: probe raised")
        return BisectResult(
            outcome="error",
            suggested_merge_plan=[f"Bisect aborted: {exc}"],
            runs_consumed=0,
            reason=str(exc),
        )

    budget_exhausted = runs >= max_runs and bool(undetermined)

    if not guilty and not undetermined:
        # Full bundle passed — no-op recommendation. The grouped PR is
        # safe to merge as-is.
        return BisectResult(
            outcome="all_green",
            safe_updates=safe,
            suggested_merge_plan=[
                f"Merge: {len(safe)} updates in grouped PR (all subsets passed CI)"
            ],
            runs_consumed=runs,
        )

    plan = synthesize_merge_plan(
        safe=safe,
        guilty=guilty,
        inconclusive=undetermined,
        budget_exhausted=budget_exhausted,
    )

    if guilty and not undetermined:
        outcome: Literal["all_green", "guilty_identified", "inconclusive", "error"] = (
            "guilty_identified"
        )
    else:
        outcome = "inconclusive"

    reason = ""
    if budget_exhausted:
        reason = "budget_exhausted"
    elif undetermined and not guilty:
        reason = "interaction_failure"

    return BisectResult(
        outcome=outcome,
        safe_updates=safe,
        guilty_updates=guilty,
        suggested_merge_plan=plan,
        runs_consumed=runs,
        reason=reason,
    )


# ────────────────────────────────────────────────────────────────────
# Comment formatter (used by the agent integration hook)
# ────────────────────────────────────────────────────────────────────


def format_bisect_comment(result: BisectResult) -> str:
    """Render a ``BisectResult`` as a PR comment body."""
    lines = [
        "## Caretaker dependency bisect",
        "",
        f"**Outcome:** `{result.outcome}`  ",
        f"**CI runs consumed:** {result.runs_consumed}",
    ]
    if result.reason:
        lines.append(f"**Reason:** `{result.reason}`")
    lines.append("")

    if result.outcome == "inconclusive" and result.reason == "not_grouped":
        lines.append(
            "This PR does not look like a grouped Dependabot bundle "
            "(fewer than 2 updates parsed); nothing to bisect."
        )
        lines.append("")
        lines.append(BISECTOR_COMMENT_MARKER)
        return "\n".join(lines)

    if result.safe_updates:
        lines.append("### Safe to merge")
        for u in result.safe_updates:
            lines.append(f"- `{u.name}` {u.from_version} → {u.to_version} ({u.directory})")
        lines.append("")

    if result.guilty_updates:
        lines.append("### Blocking CI")
        for u in result.guilty_updates:
            lines.append(f"- `{u.name}` {u.from_version} → {u.to_version} ({u.directory})")
        lines.append("")

    if result.suggested_merge_plan:
        lines.append("### Suggested plan")
        for step in result.suggested_merge_plan:
            lines.append(f"1. {step}")
        lines.append("")

    lines.append(BISECTOR_COMMENT_MARKER)
    return "\n".join(lines)
