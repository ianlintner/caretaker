"""Issue triage — close empty/duplicate/stale/resolved issues.

Mirrors the PR triage module for issues. Groups by CVE / package / title hash
to find duplicates; closes empty stubs; marks stale issues per the configured
cutoff; and backstops GitHub's ``Fixes #N`` auto-close for bot-edited PRs.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from caretaker.config import TriageConfig
    from caretaker.github_client.api import GitHubClient
    from caretaker.github_client.models import Issue, PullRequest
    from caretaker.state.models import TrackedIssue

logger = logging.getLogger(__name__)


_CVE_RE = re.compile(r"CVE-\d{4}-\d+", re.IGNORECASE)


@dataclass
class IssueTriageReport:
    closed_empty: list[int] = field(default_factory=list)
    closed_duplicate: list[int] = field(default_factory=list)
    closed_stale: list[int] = field(default_factory=list)
    closed_resolved: list[int] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _is_empty_issue(issue: Issue) -> bool:
    """Return True when the issue has no substantive body."""
    body = (issue.body or "").strip()
    if len(body) < 20:
        return True
    # Body composed only of bot boilerplate / checklist markers.
    meaningful = re.sub(r"[-*\[\]\s`]|(?:TODO|N/A)", "", body, flags=re.IGNORECASE)
    return len(meaningful) < 10


def _group_key(issue: Issue) -> str | None:
    """Return grouping key for duplicate detection."""
    haystack = f"{issue.title}\n{issue.body}"
    cve = _CVE_RE.search(haystack)
    if cve:
        return cve.group(0).upper()
    title = issue.title.strip().lower()
    if not title:
        return None
    # Hash normalized title — punctuation + whitespace collapsed.
    normalized = re.sub(r"\s+", " ", re.sub(r"[^\w\s]", "", title))
    if len(normalized) < 10:
        return None
    return "t:" + hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:12]


async def _close_issue(
    github: GitHubClient,
    owner: str,
    repo: str,
    number: int,
    reason: str,
) -> bool:
    try:
        await github.add_issue_comment(owner, repo, number, f"Closing: {reason}")
        await github.update_issue(owner, repo, number, state="closed")
        return True
    except Exception as exc:
        logger.warning("Failed to close issue #%d: %s", number, exc)
        return False


_QA_SCENARIO_MARKER = "<!-- caretaker:qa-scenario -->"


def is_qa_scenario_issue(issue: Issue) -> bool:
    """Return True when the issue body contains the QA-scenario suppression marker.

    Issues created by caretaker-qa embed this marker to prevent caretaker from
    triaging, dispatching, or escalating them. The marker is checked in both
    the issue body and (for belt-and-suspenders safety) issue title.
    """
    haystack = f"{issue.title}\n{issue.body or ''}"
    return _QA_SCENARIO_MARKER in haystack


async def close_empty_issues(
    github: GitHubClient,
    owner: str,
    repo: str,
    open_issues: list[Issue],
    *,
    dry_run: bool = False,
) -> list[int]:
    """Close issues with empty bodies or bot-generated stubs."""
    closed: list[int] = []
    for issue in open_issues:
        if not _is_empty_issue(issue):
            continue
        # Protect human-authored issues with labels — only bot/empty stubs.
        if issue.has_label("pinned") or issue.has_label("keep-open"):
            continue
        # Never close QA-scenario issues — they are synthetic test fixtures.
        if is_qa_scenario_issue(issue):
            continue
        reason = "issue body is empty or contains only boilerplate; no actionable content."
        if dry_run:
            closed.append(issue.number)
            continue
        if await _close_issue(github, owner, repo, issue.number, reason):
            closed.append(issue.number)
    return closed


async def close_duplicate_issues(
    github: GitHubClient,
    owner: str,
    repo: str,
    open_issues: list[Issue],
    *,
    dry_run: bool = False,
) -> list[int]:
    """Group by CVE / normalized title hash; close stragglers as duplicates.

    Survivor: oldest issue by ``created_at`` (preserves the canonical history).
    """
    closed: list[int] = []

    groups: dict[str, list[Issue]] = {}
    for issue in open_issues:
        if is_qa_scenario_issue(issue):
            continue
        key = _group_key(issue)
        if key is None:
            continue
        groups.setdefault(key, []).append(issue)

    for key, issues in groups.items():
        if len(issues) < 2:
            continue

        def _sort_key(i: Issue) -> tuple[float, int]:
            created = i.created_at.timestamp() if i.created_at else float("inf")
            return (created, i.number)

        survivor = min(issues, key=_sort_key)
        for issue in issues:
            if issue.number == survivor.number:
                continue
            reason = f"duplicate of #{survivor.number} (both match {key})."
            if dry_run:
                closed.append(issue.number)
                continue
            if await _close_issue(github, owner, repo, issue.number, reason):
                closed.append(issue.number)
    return closed


async def mark_stale_issues(
    github: GitHubClient,
    owner: str,
    repo: str,
    open_issues: list[Issue],
    stale_days: int,
    *,
    dry_run: bool = False,
) -> list[int]:
    """Close issues untouched for more than ``stale_days`` days.

    Respects a ``pinned`` or ``keep-open`` label as an opt-out.
    """
    if stale_days <= 0:
        return []
    closed: list[int] = []
    cutoff = datetime.now(UTC) - timedelta(days=stale_days)

    for issue in open_issues:
        if issue.has_label("pinned") or issue.has_label("keep-open"):
            continue
        if is_qa_scenario_issue(issue):
            continue
        last_touched = issue.updated_at or issue.created_at
        if last_touched is None:
            continue
        if last_touched.tzinfo is None:
            last_touched = last_touched.replace(tzinfo=UTC)
        if last_touched > cutoff:
            continue
        reason = f"no activity in {stale_days} days; closing as stale."
        if dry_run:
            closed.append(issue.number)
            continue
        if await _close_issue(github, owner, repo, issue.number, reason):
            closed.append(issue.number)
    return closed


async def close_resolved_issues(
    github: GitHubClient,
    owner: str,
    repo: str,
    open_issues: list[Issue],
    merged_prs: list[PullRequest],
    tracked_issues: dict[int, TrackedIssue],
    *,
    dry_run: bool = False,
) -> list[int]:
    """Backstop: close issues whose linked PR merged but auto-close missed.

    Consults the GraphQL ``closingIssuesReferences`` connection for the merged
    PR (via the existing ``get_closing_issue_numbers`` helper) and cross-checks
    against ``TrackedIssue.assigned_pr``. Any open issue that matches is
    closed with a reference to the merge commit.
    """
    closed: list[int] = []
    open_numbers = {i.number for i in open_issues}

    # Build a map of issue → merged PR from the GraphQL closing-issues data.
    issue_to_pr: dict[int, int] = {}
    for pr in merged_prs:
        if not pr.merged:
            continue
        try:
            linked = await github.get_closing_issue_numbers(owner, repo, pr.number)
        except Exception as exc:
            logger.warning("get_closing_issue_numbers failed for PR #%d: %s", pr.number, exc)
            continue
        for num in linked:
            issue_to_pr.setdefault(num, pr.number)

    # Also honor TrackedIssue.assigned_pr when the PR is marked merged.
    merged_numbers = {pr.number for pr in merged_prs if pr.merged}
    for issue_number, tracked in tracked_issues.items():
        if tracked.assigned_pr in merged_numbers:
            assert tracked.assigned_pr is not None
            issue_to_pr.setdefault(issue_number, tracked.assigned_pr)

    for issue_number, pr_number in issue_to_pr.items():
        if issue_number not in open_numbers:
            continue
        reason = f"resolved by merged PR #{pr_number}."
        if dry_run:
            closed.append(issue_number)
            continue
        if await _close_issue(github, owner, repo, issue_number, reason):
            closed.append(issue_number)
    return closed


async def run_issue_triage(
    github: GitHubClient,
    owner: str,
    repo: str,
    open_issues: list[Issue],
    merged_prs: list[PullRequest],
    tracked_issues: dict[int, TrackedIssue],
    config: TriageConfig,
) -> IssueTriageReport:
    """Run the full issue triage pass and return a report."""
    report = IssueTriageReport()
    if not config.enabled or not config.issue_triage:
        return report

    try:
        report.closed_empty = await close_empty_issues(
            github, owner, repo, open_issues, dry_run=config.dry_run
        )
    except Exception as exc:
        report.errors.append(f"close_empty_issues: {exc}")

    try:
        report.closed_duplicate = await close_duplicate_issues(
            github, owner, repo, open_issues, dry_run=config.dry_run
        )
    except Exception as exc:
        report.errors.append(f"close_duplicate_issues: {exc}")

    try:
        report.closed_resolved = await close_resolved_issues(
            github,
            owner,
            repo,
            open_issues,
            merged_prs,
            tracked_issues,
            dry_run=config.dry_run,
        )
    except Exception as exc:
        report.errors.append(f"close_resolved_issues: {exc}")

    try:
        report.closed_stale = await mark_stale_issues(
            github,
            owner,
            repo,
            open_issues,
            config.stale_issue_days,
            dry_run=config.dry_run,
        )
    except Exception as exc:
        report.errors.append(f"mark_stale_issues: {exc}")

    return report
