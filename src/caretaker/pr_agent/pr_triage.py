"""PR triage — close empty/duplicate/pointless PRs and merge valid drafts.

Encodes the cleanup pass Claude Code ran manually on 2026-04-21 (see
memory/project_pr_triage.md). Runs over the open PR list and emits a
``PRTriageReport`` summarizing what it did. All close actions post an
explanatory comment first so humans reading the PR can follow why.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from caretaker.pr_agent.states import CIStatus, evaluate_ci

if TYPE_CHECKING:
    from caretaker.config import TriageConfig
    from caretaker.github_client.api import GitHubClient
    from caretaker.github_client.models import PullRequest

logger = logging.getLogger(__name__)


_CVE_RE = re.compile(r"CVE-\d{4}-\d+", re.IGNORECASE)
_PACKAGE_BUMP_RE = re.compile(
    r"\b(?:bump|upgrade|update)\s+(?P<pkg>[a-zA-Z0-9_.-]+)",
    re.IGNORECASE,
)
# Match "to vX.Y.Z" (with or without the v) anywhere in a PR title — used to
# extract the target version of an upgrade PR so dedup prefers the newer
# target over the older. F-6 from the 2026-04-27 QA cycle: caretaker-qa#67
# (target v0.25.0) was incorrectly closed as a duplicate of caretaker-qa#39
# (target v0.19.4) because the dedup heuristic picked oldest-by-created_at,
# stalling the upgrade chain.
_TARGET_VERSION_RE = re.compile(
    r"\bto\s+v?(?P<ver>\d+(?:\.\d+){1,3}(?:[a-zA-Z0-9.+-]*))",
    re.IGNORECASE,
)


def _parse_target_version(title: str | None) -> tuple[int, ...] | None:
    """Extract a comparable version tuple from an upgrade PR title.

    Returns ``None`` when the title doesn't carry a "to vX.Y.Z" clause
    (or it can't be parsed cleanly), so callers can fall back to a
    timestamp tiebreak. Pre-release / build suffixes after the numeric
    components are dropped — comparing v1.2.3-rc1 against v1.2.3 as
    equal is acceptable for dedup purposes.
    """
    if not title:
        return None
    match = _TARGET_VERSION_RE.search(title)
    if not match:
        return None
    raw = match.group("ver")
    parts: list[int] = []
    for chunk in raw.split("."):
        # Strip any non-numeric trailing junk (e.g. "3-rc1" → "3").
        digits = re.match(r"\d+", chunk)
        if not digits:
            break
        parts.append(int(digits.group(0)))
    return tuple(parts) if parts else None


@dataclass
class PRTriageReport:
    closed_empty: list[int] = field(default_factory=list)
    closed_duplicate: list[int] = field(default_factory=list)
    closed_conflicted: list[int] = field(default_factory=list)
    readied: list[int] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _is_binary_only_diff(files: list[dict[str, object]], binary_paths: list[str]) -> bool:
    """Return True if every changed file in the diff is a known binary path."""
    if not files:
        return False
    binary_set = set(binary_paths)
    for f in files:
        path = str(f.get("path", ""))
        if path not in binary_set:
            return False
    return True


def _is_empty_pr_body(pr: PullRequest) -> bool:
    """Return True when the PR has no substantive description.

    A PR body is considered empty when it is blank, shorter than 20 characters,
    or contains only checklist / boilerplate markup with no real text.
    """
    body = (pr.body or "").strip()
    if len(body) < 20:
        return True
    # Body composed only of checklist markers, whitespace, or common placeholders.
    meaningful = re.sub(
        r"[-*\[\]\s`]|(?:TODO|N/A|<!--.*?-->)", "", body, flags=re.IGNORECASE | re.DOTALL
    )
    return len(meaningful) < 10


def _extract_group_key(pr: PullRequest) -> str | None:
    """Return a grouping key for duplicate detection — CVE id or bumped package."""
    haystack = f"{pr.title}\n{pr.body}"
    cve = _CVE_RE.search(haystack)
    if cve:
        return cve.group(0).upper()
    pkg = _PACKAGE_BUMP_RE.search(pr.title)
    if pkg:
        return f"pkg:{pkg.group('pkg').lower()}"
    return None


async def _post_close_comment(
    github: GitHubClient,
    owner: str,
    repo: str,
    pr_number: int,
    reason: str,
    *,
    supersedes: int | None = None,
) -> None:
    parts = [f"Closing: {reason}"]
    if supersedes is not None:
        parts.append(f"Superseded by #{supersedes}.")
    body = "\n\n".join(parts)
    try:
        await github.add_issue_comment(owner, repo, pr_number, body)
    except Exception as exc:
        logger.warning("Failed to post close comment on PR #%d: %s", pr_number, exc)


async def close_empty_prs(
    github: GitHubClient,
    owner: str,
    repo: str,
    open_prs: list[PullRequest],
    binary_only_paths: list[str],
    *,
    dry_run: bool = False,
) -> list[int]:
    """Close PRs whose entire diff is binary-only state files."""
    closed: list[int] = []
    for pr in open_prs:
        try:
            files = await github.list_pull_request_files(owner, repo, pr.number)
        except Exception as exc:
            logger.warning("list_pull_request_files failed for #%d: %s", pr.number, exc)
            continue
        if not _is_binary_only_diff(files, binary_only_paths):
            continue
        reason = (
            "diff contains only binary state files "
            f"({', '.join(sorted(binary_only_paths))}); no meaningful code changes."
        )
        if dry_run:
            closed.append(pr.number)
            continue
        await _post_close_comment(github, owner, repo, pr.number, reason)
        try:
            await github.update_issue(owner, repo, pr.number, state="closed")
            closed.append(pr.number)
        except Exception as exc:
            logger.warning("Failed to close empty PR #%d: %s", pr.number, exc)
    return closed


async def close_empty_body_prs(
    github: GitHubClient,
    owner: str,
    repo: str,
    open_prs: list[PullRequest],
    *,
    dry_run: bool = False,
) -> list[int]:
    """Close PRs that have no substantive description in their body.

    A PR body is considered empty when it is blank or contains only boilerplate
    (checklists, HTML comments, placeholder text). This heuristic applies
    regardless of PR author so that QA scenario PRs with placeholder bodies
    (e.g. ``qa/scenario-10-empty-pr``) are caught even when opened by the
    repo owner.
    """
    closed: list[int] = []
    for pr in open_prs:
        if not _is_empty_pr_body(pr):
            continue
        reason = "PR description is empty or contains only boilerplate; please add context."
        if dry_run:
            closed.append(pr.number)
            continue
        await _post_close_comment(github, owner, repo, pr.number, reason)
        try:
            await github.update_issue(owner, repo, pr.number, state="closed")
            closed.append(pr.number)
        except Exception as exc:
            logger.warning("Failed to close empty-body PR #%d: %s", pr.number, exc)
    return closed


async def close_duplicate_fix_prs(
    github: GitHubClient,
    owner: str,
    repo: str,
    open_prs: list[PullRequest],
    *,
    dry_run: bool = False,
) -> list[int]:
    """Group security/fix PRs by CVE or bumped package; close the stragglers.

    Survivor selection: oldest PR wins by ``created_at`` (lowest PR number as
    tie-break). The canonical review history — inline comments, CI diagnoses,
    reviewer discussion — lives on the first PR opened against a given fix;
    closing the older twin would discard that context. Aligned with the
    sibling policy in
    :func:`caretaker.issue_agent.issue_triage.close_duplicate_issues`.
    """
    closed: list[int] = []

    groups: dict[str, list[PullRequest]] = {}
    for pr in open_prs:
        key = _extract_group_key(pr)
        if key is None:
            continue
        groups.setdefault(key, []).append(pr)

    for key, prs in groups.items():
        if len(prs) < 2:
            continue

        if key.startswith("pkg:"):
            # Upgrade PRs: prefer the highest target version, then newest by
            # created_at as tiebreak. Closing the newer target in favor of
            # an older stale upgrade PR was F-6 — it stalled the upgrade
            # chain on caretaker-qa where a v0.19.4 PR sat open for two
            # days and silently closed every v0.25.0 bump that came after.
            #
            # PRs without a parseable target version sort behind any PR
            # that does parse, so a freshly-titled "upgrade caretaker
            # pin from v0.24.0 to v0.25.0" beats an unparseable
            # placeholder title. Lowest PR number breaks the final tie
            # so two same-target same-time PRs collapse deterministically.
            def _pkg_sort_key(p: PullRequest) -> tuple[tuple[int, ...], float, int]:
                version = _parse_target_version(p.title) or ()
                created = p.created_at.timestamp() if p.created_at else 0.0
                # Negate so ``min`` selects the highest version + newest.
                return (
                    tuple(-c for c in version) if version else (1,),
                    -created,
                    p.number,
                )

            survivor = min(prs, key=_pkg_sort_key)
        else:
            # CVE / non-upgrade groups: oldest wins, mirroring
            # issue_agent.issue_triage.close_duplicate_issues — canonical
            # review history lives on the first PR opened for a given fix.
            def _oldest_sort_key(p: PullRequest) -> tuple[float, int]:
                created = p.created_at.timestamp() if p.created_at else float("inf")
                return (created, p.number)

            survivor = min(prs, key=_oldest_sort_key)
        for pr in prs:
            if pr.number == survivor.number:
                continue
            reason = f"duplicate of #{survivor.number} (both address {key})."
            if dry_run:
                closed.append(pr.number)
                continue
            await _post_close_comment(
                github, owner, repo, pr.number, reason, supersedes=survivor.number
            )
            try:
                await github.update_issue(owner, repo, pr.number, state="closed")
                closed.append(pr.number)
            except Exception as exc:
                logger.warning("Failed to close duplicate PR #%d: %s", pr.number, exc)
    return closed


async def close_binary_conflicted_prs(
    github: GitHubClient,
    owner: str,
    repo: str,
    open_prs: list[PullRequest],
    binary_only_paths: list[str],
    *,
    dry_run: bool = False,
) -> list[int]:
    """Close PRs whose *only* merge conflict is a binary state file.

    The 2026-04-21 cleanup applied the meaningful code diff directly to main
    and closed the PR. Auto-applying a diff programmatically is risky, so this
    implementation conservatively *closes* the PR with a note asking for a
    rebase; the human (or a follow-up automation) can re-apply.
    """
    closed: list[int] = []
    binary_set = set(binary_only_paths)
    for pr in open_prs:
        if pr.mergeable is not False:
            continue
        try:
            files = await github.list_pull_request_files(owner, repo, pr.number)
        except Exception as exc:
            logger.warning("list_pull_request_files failed for #%d: %s", pr.number, exc)
            continue
        touched = {str(f.get("path", "")) for f in files}
        if not touched:
            continue
        # Every conflicted touch must be a binary state file; at least one real
        # file must exist (otherwise close_empty_prs would have caught it).
        conflict_paths = touched & binary_set
        real_paths = touched - binary_set
        if not conflict_paths or not real_paths:
            continue
        reason = (
            "merge conflict caused by binary state file(s) "
            f"({', '.join(sorted(conflict_paths))}). These files are now gitignored; "
            "please rebase or reopen the PR with a clean branch."
        )
        if dry_run:
            closed.append(pr.number)
            continue
        await _post_close_comment(github, owner, repo, pr.number, reason)
        try:
            await github.update_issue(owner, repo, pr.number, state="closed")
            closed.append(pr.number)
        except Exception as exc:
            logger.warning("Failed to close conflicted PR #%d: %s", pr.number, exc)
    return closed


async def ready_valid_copilot_drafts(
    github: GitHubClient,
    owner: str,
    repo: str,
    open_prs: list[PullRequest],
    *,
    dry_run: bool = False,
) -> list[int]:
    """Mark Copilot and caretaker draft PRs ready-for-review when CI is green.

    After promoting a caretaker-authored PR (``claude/`` / ``caretaker/``
    branch prefix), also requests a Copilot review so the PR enters the
    review → merge cycle without manual intervention.

    Does not merge — delegates to the existing ``merge.evaluate_merge`` flow
    that runs later in the PR agent cycle. Only flips the draft bit.
    """
    readied: list[int] = []
    for pr in open_prs:
        if not pr.draft or not (pr.is_copilot_pr or pr.is_caretaker_pr):
            continue
        try:
            check_runs = await github.get_check_runs(owner, repo, pr.head_sha)
        except Exception as exc:
            logger.warning("get_check_runs failed for #%d: %s", pr.number, exc)
            continue
        ci = evaluate_ci(check_runs)
        if ci.status != CIStatus.PASSING or not ci.all_completed:
            continue
        if dry_run:
            readied.append(pr.number)
            continue
        try:
            if not pr.node_id:
                logger.warning(
                    "PR #%d has no node_id — cannot flip draft bit via GraphQL; skipping",
                    pr.number,
                )
                continue
            ok = await github.mark_pull_request_ready(pr.node_id)
            if ok:
                logger.info("Marked draft PR #%d ready-for-review", pr.number)
                readied.append(pr.number)
                if pr.is_caretaker_pr:
                    try:
                        await github.request_reviewers(
                            owner, repo, pr.number, ["copilot-pull-request-reviewer"]
                        )
                        logger.info("Requested Copilot review for caretaker PR #%d", pr.number)
                    except Exception as review_exc:
                        logger.warning(
                            "Failed to request Copilot review for PR #%d: %s",
                            pr.number,
                            review_exc,
                        )
            else:
                logger.warning("mark_pull_request_ready returned False for PR #%d", pr.number)
        except Exception as exc:
            logger.warning("Failed to ready draft PR #%d: %s", pr.number, exc)
    return readied


async def run_pr_triage(
    github: GitHubClient,
    owner: str,
    repo: str,
    open_prs: list[PullRequest],
    config: TriageConfig,
) -> PRTriageReport:
    """Run the full PR triage pass and return a report."""
    report = PRTriageReport()
    if not config.enabled or not config.pr_triage:
        return report

    # Order matters: empty/conflicted/duplicate reduce the working set before
    # ready_valid_copilot_drafts considers what remains.
    try:
        report.closed_empty = await close_empty_prs(
            github, owner, repo, open_prs, config.binary_only_paths, dry_run=config.dry_run
        )
    except Exception as exc:
        report.errors.append(f"close_empty_prs: {exc}")

    try:
        empty_body = await close_empty_body_prs(
            github, owner, repo, open_prs, dry_run=config.dry_run
        )
        report.closed_empty.extend(empty_body)
    except Exception as exc:
        report.errors.append(f"close_empty_body_prs: {exc}")

    try:
        report.closed_conflicted = await close_binary_conflicted_prs(
            github, owner, repo, open_prs, config.binary_only_paths, dry_run=config.dry_run
        )
    except Exception as exc:
        report.errors.append(f"close_binary_conflicted_prs: {exc}")

    try:
        report.closed_duplicate = await close_duplicate_fix_prs(
            github, owner, repo, open_prs, dry_run=config.dry_run
        )
    except Exception as exc:
        report.errors.append(f"close_duplicate_fix_prs: {exc}")

    try:
        report.readied = await ready_valid_copilot_drafts(
            github, owner, repo, open_prs, dry_run=config.dry_run
        )
    except Exception as exc:
        report.errors.append(f"ready_valid_copilot_drafts: {exc}")

    return report
