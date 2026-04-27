"""Charlie Agent — janitorial cleanup for caretaker-managed work."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from caretaker.causal import make_causal_marker, parent_from_body

if TYPE_CHECKING:
    from caretaker.github_client.api import GitHubClient
    from caretaker.github_client.models import Issue, PullRequest

logger = logging.getLogger(__name__)

TRACKING_ISSUE_TITLE = "[Maintainer] Orchestrator State"

_MANAGED_ISSUE_LABELS = frozenset(
    {
        "maintainer:assigned",
        "maintainer:internal",
        "devops:build-failure",
        "caretaker:self-heal",
        "maintainer:escalation-digest",
    }
)
_MANAGED_ISSUE_MARKERS = (
    "<!-- caretaker:assignment -->",
    "<!-- caretaker:devops-build-failure",
    "<!-- caretaker:self-heal -->",
    "<!-- caretaker:escalation-digest",
    "<!-- maintainer-state:",
)
_MANAGED_PR_MARKERS = _MANAGED_ISSUE_MARKERS
_DEFAULT_EXEMPT_LABELS = frozenset(
    {
        "pinned",
        "maintainer:escalated",
        "maintainer:escalation-digest",
    }
)

_SOURCE_ISSUE_RE = re.compile(r"\bSOURCE_ISSUE:\s*(?:[\w.-]+/[\w.-]+)?#(\d+)\b", re.IGNORECASE)
_FIXES_RE = re.compile(r"\bfixes\s+(?:[\w.-]+/[\w.-]+)?#(\d+)\b", re.IGNORECASE)
_DEVOPS_SIG_RE = re.compile(r"caretaker:devops-build-failure\s+sig:([0-9a-f]+)", re.IGNORECASE)
_ESCALATION_WEEK_RE = re.compile(
    r"caretaker:escalation-digest\s+week:([0-9]{4}-W\d+)", re.IGNORECASE
)
_RUN_ID_RE = re.compile(r"\brun_id:(\d+)\b")


@dataclass
class CharlieReport:
    """Results from a single Charlie agent run."""

    managed_issues_seen: int = 0
    managed_prs_seen: int = 0
    issues_closed: int = 0
    prs_closed: int = 0
    duplicate_issues_closed: int = 0
    duplicate_prs_closed: int = 0
    stale_issues_closed: int = 0
    stale_prs_closed: int = 0
    comments_posted: int = 0
    errors: list[str] = field(default_factory=list)


class CharlieAgent:
    """Janitorial cleanup for caretaker-managed issues and pull requests."""

    def __init__(
        self,
        github: GitHubClient,
        owner: str,
        repo: str,
        stale_days: int = 14,
        close_duplicate_issues: bool = True,
        close_duplicate_prs: bool = True,
        close_stale_issues: bool = True,
        close_stale_prs: bool = True,
        exempt_labels: list[str] | None = None,
    ) -> None:
        self._github = github
        self._owner = owner
        self._repo = repo
        self._stale_days = stale_days
        self._close_duplicate_issues = close_duplicate_issues
        self._close_duplicate_prs = close_duplicate_prs
        self._close_stale_issues = close_stale_issues
        self._close_stale_prs = close_stale_prs
        self._exempt_labels = _DEFAULT_EXEMPT_LABELS | set(exempt_labels or [])

    async def run(self) -> CharlieReport:
        """Run Charlie cleanup against caretaker-managed work."""
        report = CharlieReport()

        try:
            issues = await self._github.list_issues(self._owner, self._repo, state="open")
            prs = await self._github.list_pull_requests(self._owner, self._repo, state="open")
        except Exception as exc:
            logger.error("Charlie agent failed to load repo state: %s", exc)
            report.errors.append(str(exc))
            return report

        managed_issues = [issue for issue in issues if self._is_managed_issue(issue)]
        managed_prs = [pr for pr in prs if self._is_managed_pr(pr)]
        report.managed_issues_seen = len(managed_issues)
        report.managed_prs_seen = len(managed_prs)

        logger.info(
            "Charlie agent: managing %d issues and %d PRs",
            report.managed_issues_seen,
            report.managed_prs_seen,
        )

        # Build issue → run_id map for cross-referencing PRs with related issues
        issue_run_map = _build_issue_run_map(managed_issues)
        # Build issue → label set map so PR fingerprinting can gate on label class
        issue_label_map = _build_issue_label_map(managed_issues)

        closed_issue_numbers: set[int] = set()
        closed_pr_numbers: set[int] = set()

        if self._close_duplicate_issues:
            closed_issue_numbers |= await self._close_duplicate_issues_in_place(
                managed_issues, report
            )
        if self._close_duplicate_prs:
            # Fingerprint Copilot-authored managed PRs by primary touched file
            # when they close a self-heal-labeled issue. Generalizes the D2
            # upgrade dedupe pattern to self-heal PR races where Copilot opens
            # N PRs for N distinct self-heal issues that all patch the same
            # underlying bug (e.g. caretaker #411/#415/#416).
            self_heal_primary_map = await self._build_self_heal_primary_map(
                managed_prs, issue_label_map
            )
            closed_pr_numbers |= await self._close_duplicate_prs_in_place(
                managed_prs,
                report,
                issue_run_map=issue_run_map,
                self_heal_primary_map=self_heal_primary_map,
            )
        if self._close_stale_issues:
            closed_issue_numbers |= await self._close_stale_issues_in_place(
                managed_issues, closed_issue_numbers, report
            )
        if self._close_stale_prs:
            closed_pr_numbers |= await self._close_stale_prs_in_place(
                managed_prs, closed_pr_numbers, report
            )

        return report

    async def _close_duplicate_issues_in_place(
        self, issues: list[Issue], report: CharlieReport
    ) -> set[int]:
        groups = self._group_issues_by_work_key(issues)
        closed: set[int] = set()

        for work_key, grouped_issues in groups.items():
            if len(grouped_issues) < 2:
                continue

            canonical = min(grouped_issues, key=self._issue_preference_key)
            for issue in grouped_issues:
                if issue.number == canonical.number:
                    continue
                causal = make_causal_marker(
                    "charlie:close-duplicate-issue",
                    parent=parent_from_body(getattr(issue, "body", "") or ""),
                )
                await self._comment_and_close_issue(
                    issue.number,
                    (
                        f"{causal}\n\n"
                        "🧹 Charlie work: closing this duplicate caretaker-managed issue in "
                        f"favor of #{canonical.number} (`{work_key}`)."
                    ),
                    report,
                )
                report.duplicate_issues_closed += 1
                closed.add(issue.number)

        return closed

    async def _close_duplicate_prs_in_place(
        self,
        prs: list[PullRequest],
        report: CharlieReport,
        *,
        issue_run_map: dict[int, str] | None = None,
        self_heal_primary_map: dict[int, str] | None = None,
    ) -> set[int]:
        groups = self._group_prs_by_work_key(
            prs,
            issue_run_map=issue_run_map,
            self_heal_primary_map=self_heal_primary_map,
        )
        closed: set[int] = set()

        for work_key, grouped_prs in groups.items():
            if len(grouped_prs) < 2:
                continue

            canonical = min(grouped_prs, key=self._pr_preference_key)
            for pr in grouped_prs:
                if pr.number == canonical.number:
                    continue
                causal = make_causal_marker(
                    "charlie:close-duplicate-pr",
                    parent=parent_from_body(getattr(pr, "body", "") or ""),
                )
                await self._comment_and_close_pr(
                    pr.number,
                    (
                        f"{causal}\n\n"
                        "🧹 Charlie work: closing this duplicate caretaker-managed PR in "
                        f"favor of #{canonical.number} (`{work_key}`)."
                    ),
                    report,
                )
                report.duplicate_prs_closed += 1
                closed.add(pr.number)

        return closed

    async def _close_stale_issues_in_place(
        self,
        issues: list[Issue],
        already_closed: set[int],
        report: CharlieReport,
    ) -> set[int]:
        closed: set[int] = set()
        now = datetime.now(UTC)

        for issue in issues:
            if issue.number in already_closed or self._is_exempt(issue):
                continue
            if issue.title == TRACKING_ISSUE_TITLE or issue.is_copilot_assigned:
                continue

            updated_at = _item_timestamp(issue)
            age_days = (now - updated_at).days
            if age_days < self._stale_days:
                continue

            causal = make_causal_marker(
                "charlie:close-stale-issue",
                parent=parent_from_body(getattr(issue, "body", "") or ""),
            )
            await self._comment_and_close_issue(
                issue.number,
                (
                    f"{causal}\n\n"
                    "🧹 Charlie work: closing this caretaker-managed issue after "
                    f"{age_days} days without meaningful activity. Reopen if it still matters."
                ),
                report,
            )
            report.stale_issues_closed += 1
            closed.add(issue.number)

        return closed

    async def _close_stale_prs_in_place(
        self,
        prs: list[PullRequest],
        already_closed: set[int],
        report: CharlieReport,
    ) -> set[int]:
        closed: set[int] = set()
        now = datetime.now(UTC)

        for pr in prs:
            if pr.number in already_closed or self._is_exempt(pr):
                continue

            updated_at = _item_timestamp(pr)
            age_days = (now - updated_at).days
            if age_days < self._stale_days:
                continue

            causal = make_causal_marker(
                "charlie:close-stale-pr",
                parent=parent_from_body(getattr(pr, "body", "") or ""),
            )
            await self._comment_and_close_pr(
                pr.number,
                (
                    f"{causal}\n\n"
                    "🧹 Charlie work: closing this caretaker-managed PR after "
                    f"{age_days} days without meaningful activity. Reopen or recreate if needed."
                ),
                report,
            )
            report.stale_prs_closed += 1
            closed.add(pr.number)

        return closed

    async def _comment_and_close_issue(self, number: int, body: str, report: CharlieReport) -> None:
        try:
            await self._github.add_issue_comment(self._owner, self._repo, number, body)
            report.comments_posted += 1
            await self._github.update_issue(self._owner, self._repo, number, state="closed")
            report.issues_closed += 1
            logger.info("Charlie agent: closed issue #%d", number)
        except Exception as exc:
            logger.warning("Charlie agent: issue #%d cleanup failed: %s", number, exc)
            report.errors.append(f"issue #{number}: {exc}")

    async def _comment_and_close_pr(self, number: int, body: str, report: CharlieReport) -> None:
        try:
            await self._github.add_issue_comment(self._owner, self._repo, number, body)
            report.comments_posted += 1
            await self._github.update_issue(self._owner, self._repo, number, state="closed")
            report.prs_closed += 1
            logger.info("Charlie agent: closed PR #%d", number)
        except Exception as exc:
            logger.warning("Charlie agent: PR #%d cleanup failed: %s", number, exc)
            report.errors.append(f"PR #{number}: {exc}")

    def _is_managed_issue(self, issue: Issue) -> bool:
        if issue.title == TRACKING_ISSUE_TITLE:
            return False

        label_names = {label.name for label in issue.labels}
        body = issue.body or ""
        if issue.title.startswith("[Maintainer]"):
            return True
        if any(marker in body for marker in _MANAGED_ISSUE_MARKERS):
            return True
        return issue.user.login == "app/github-actions" and bool(
            label_names & _MANAGED_ISSUE_LABELS
        )

    def _is_managed_pr(self, pr: PullRequest) -> bool:
        body = pr.body or ""
        return (
            pr.is_copilot_pr
            or pr.is_maintainer_pr
            or any(marker in body for marker in _MANAGED_PR_MARKERS)
        )

    def _is_exempt(self, item: Issue | PullRequest) -> bool:
        label_names = {label.name for label in item.labels}
        return bool(label_names & self._exempt_labels)

    @staticmethod
    def _group_issues_by_work_key(items: list[Issue]) -> dict[str, list[Issue]]:
        grouped: dict[str, list[Issue]] = {}
        for item in items:
            work_key = _extract_work_key(item.title, item.body or "")
            if work_key is None:
                continue
            grouped.setdefault(work_key, []).append(item)
        return grouped

    @staticmethod
    def _group_prs_by_work_key(
        items: list[PullRequest],
        *,
        issue_run_map: dict[int, str] | None = None,
        self_heal_primary_map: dict[int, str] | None = None,
    ) -> dict[str, list[PullRequest]]:
        grouped: dict[str, list[PullRequest]] = {}
        for item in items:
            work_key = _extract_work_key(item.title, item.body or "")
            if work_key is None and issue_run_map:
                work_key = _resolve_pr_run_key(item.body or "", issue_run_map)
            if work_key is None and self_heal_primary_map:
                primary = self_heal_primary_map.get(item.number)
                if primary:
                    work_key = f"self_heal_primary:{primary}"
            if work_key is None:
                continue
            grouped.setdefault(work_key, []).append(item)
        # Merge groups whose linked issues share a workflow run_id
        if issue_run_map:
            grouped = _merge_pr_groups_by_run(grouped, issue_run_map)
        return grouped

    async def _build_self_heal_primary_map(
        self,
        prs: list[PullRequest],
        issue_label_map: dict[int, frozenset[str]],
    ) -> dict[int, str]:
        """Fingerprint managed Copilot PRs by primary source file.

        Returns ``{pr_number: primary_file_path}`` for each Copilot-authored
        PR that (a) closes at least one issue labeled ``caretaker:self-heal``
        and (b) touches at least one non-test, non-doc source file. The
        primary file is the one with the largest ``additions+deletions``
        count, tiebroken by path.
        """
        result: dict[int, str] = {}
        for pr in prs:
            if not pr.is_copilot_pr:
                continue
            try:
                closing = await self._github.get_closing_issue_numbers(
                    self._owner, self._repo, pr.number
                )
            except Exception as exc:
                logger.warning(
                    "Charlie agent: failed to fetch closing issues for PR #%d: %s",
                    pr.number,
                    exc,
                )
                continue
            closes_self_heal = any(
                "caretaker:self-heal" in issue_label_map.get(n, frozenset()) for n in closing
            )
            if not closes_self_heal:
                continue
            try:
                files = await self._github.list_pull_request_files(
                    self._owner, self._repo, pr.number
                )
            except Exception as exc:
                logger.warning(
                    "Charlie agent: failed to fetch files for PR #%d: %s",
                    pr.number,
                    exc,
                )
                continue
            primary = _primary_source_file(files)
            if primary:
                result[pr.number] = primary
        return result

    @staticmethod
    def _issue_preference_key(issue: Issue) -> tuple[int, datetime, int]:
        has_assignee = 0 if issue.assignees else 1
        return (has_assignee, _item_timestamp(issue), issue.number)

    @staticmethod
    def _pr_preference_key(pr: PullRequest) -> tuple[int, datetime, int]:
        is_draft = 1 if pr.draft else 0
        return (is_draft, _item_timestamp(pr), pr.number)


def _extract_work_key(title: str, body: str) -> str | None:
    for prefix, pattern in (
        ("source_issue", _SOURCE_ISSUE_RE),
        ("fixes", _FIXES_RE),
        ("run_id", _RUN_ID_RE),
        ("devops_sig", _DEVOPS_SIG_RE),
        ("escalation_week", _ESCALATION_WEEK_RE),
    ):
        match = pattern.search(body)
        if match:
            return f"{prefix}:{match.group(1)}"

    normalized_title = title.casefold().strip()
    if normalized_title.startswith("[wip] fix ci failure on main"):
        return f"wip_ci:{normalized_title}"
    return None


def _item_timestamp(item: Issue | PullRequest) -> datetime:
    timestamp = item.updated_at or item.created_at or datetime.now(UTC)
    if timestamp.tzinfo is None:
        return timestamp.replace(tzinfo=UTC)
    return timestamp.astimezone(UTC)


def _build_issue_run_map(issues: list[Issue]) -> dict[int, str]:
    """Map issue number → run_id for issues that have a workflow run_id marker.

    This enables cross-referencing: PRs that fix issues from the same workflow
    run can be grouped together even though each PR references a different issue.
    """
    result: dict[int, str] = {}
    for issue in issues:
        body = issue.body or ""
        match = _RUN_ID_RE.search(body)
        if match:
            result[issue.number] = match.group(1)
    return result


def _resolve_pr_run_key(pr_body: str, issue_run_map: dict[int, str]) -> str | None:
    """Try to derive a run-based work key for a PR via its linked issue.

    If the PR body contains ``Fixes #N`` and issue *N* has a ``run_id``, the
    returned key groups all PRs that fix issues from the same workflow run.
    """
    match = _FIXES_RE.search(pr_body)
    if not match:
        return None
    issue_number = int(match.group(1))
    run_id = issue_run_map.get(issue_number)
    if run_id is None:
        return None
    return f"run_id:{run_id}"


def _merge_pr_groups_by_run(
    groups: dict[str, list[PullRequest]],
    issue_run_map: dict[int, str],
) -> dict[str, list[PullRequest]]:
    """Merge PR groups whose linked issues share a workflow run_id.

    When two PRs reference different issues (``Fixes #A`` and ``Fixes #B``)
    but both issues originate from the same workflow run, the PRs should be
    treated as duplicates.  This function merges their groups under a single
    ``run_id:<id>`` key.
    """
    # Map each fixes:N key to its run_id (if any)
    run_to_keys: dict[str, list[str]] = {}
    for key in list(groups):
        if not key.startswith("fixes:"):
            continue
        try:
            issue_num = int(key.split(":", 1)[1])
        except (ValueError, IndexError):
            continue
        run_id = issue_run_map.get(issue_num)
        if run_id:
            run_to_keys.setdefault(run_id, []).append(key)

    merged = dict(groups)
    for run_id, keys in run_to_keys.items():
        if len(keys) < 2:
            continue
        canonical_key = f"run_id:{run_id}"
        combined: list[PullRequest] = []
        for key in keys:
            combined.extend(merged.pop(key, []))
        if combined:
            merged.setdefault(canonical_key, []).extend(combined)

    return merged


def _build_issue_label_map(issues: list[Issue]) -> dict[int, frozenset[str]]:
    """Map issue number → frozenset of label names."""
    return {issue.number: frozenset(lbl.name for lbl in issue.labels) for issue in issues}


_NON_SOURCE_PREFIXES = ("tests/", "test/", "docs/", "doc/")
_NON_SOURCE_SUFFIXES = (".md", ".rst", ".txt", ".lock")


def _primary_source_file(files: list[dict[str, object]]) -> str | None:
    """Return the most-changed non-test, non-doc source file path, or ``None``.

    Filters out test, doc, and lockfile entries so two PRs whose only
    overlap is an incidental ``tests/`` touch don't get dedupe-merged.
    Tie-breaks ties by path alphabetically for deterministic grouping.
    """
    candidates: list[tuple[int, str]] = []
    for f in files:
        path_obj = f.get("path", "")
        path = path_obj if isinstance(path_obj, str) else ""
        if not path:
            continue
        if any(path.startswith(p) for p in _NON_SOURCE_PREFIXES):
            continue
        if any(path.endswith(s) for s in _NON_SOURCE_SUFFIXES):
            continue
        adds_obj = f.get("additions", 0)
        dels_obj = f.get("deletions", 0)
        adds = int(adds_obj) if isinstance(adds_obj, int | str) else 0
        dels = int(dels_obj) if isinstance(dels_obj, int | str) else 0
        candidates.append((adds + dels, path))
    if not candidates:
        return None
    candidates.sort(key=lambda c: (-c[0], c[1]))
    return candidates[0][1]
