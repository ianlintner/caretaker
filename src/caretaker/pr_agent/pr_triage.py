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

        # Survivor: oldest by created_at, falling back to lowest PR number.
        # PRs with an unknown created_at sort last (never chosen as survivor
        # over a PR with a real timestamp).
        def _sort_key(p: PullRequest) -> tuple[float, int]:
            created = p.created_at.timestamp() if p.created_at else float("inf")
            return (created, p.number)

        # why oldest wins: mirrors close_duplicate_issues in
        # issue_agent/issue_triage.py — canonical history lives on the first
        # PR opened for a given fix.
        survivor = min(prs, key=_sort_key)
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
    """Mark Copilot draft PRs ready-for-review when CI is green.

    Does not merge — delegates to the existing ``merge.evaluate_merge`` flow
    that runs later in the PR agent cycle. Only flips the draft bit.
    """
    readied: list[int] = []
    for pr in open_prs:
        if not pr.draft or not pr.is_copilot_pr:
            continue
        try:
            status = await github.get_combined_status(owner, repo, pr.head_sha)
        except Exception as exc:
            logger.warning("get_combined_status failed for #%d: %s", pr.number, exc)
            continue
        if status != "success":
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
