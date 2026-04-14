"""Stale Agent — closes inactive issues/PRs and prunes merged branches."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from caretaker.github_client.api import GitHubClient

logger = logging.getLogger(__name__)

STALE_LABEL = "stale"
_STALE_EXEMPT_LABELS = frozenset(
    {
        "pinned",
        "security",
        "security:finding",
        "dependencies:major-upgrade",
        "caretaker:self-heal",
        "devops:build-failure",
    }
)
_STALE_WARNING = (
    "This issue has been inactive for {days} days and will be closed in "
    "{close_in} days if there is no new activity. "
    "Remove the `stale` label or comment to keep it open.\n\n"
    "<!-- caretaker:stale-warning -->"
)


@dataclass
class StaleReport:
    issues_warned: int = 0
    issues_closed: int = 0
    prs_closed: int = 0
    branches_deleted: int = 0
    errors: list[str] = field(default_factory=list)


class StaleAgent:
    """
    Housekeeping agent:
    * Marks issues stale after *stale_days* of inactivity.
    * Closes stale issues after *close_after* more days.
    * Closes stale draft / abandoned PRs.
    * Deletes branches whose PRs have been merged.
    """

    def __init__(
        self,
        github: GitHubClient,
        owner: str,
        repo: str,
        stale_days: int = 60,
        close_after: int = 14,
        close_stale_prs: bool = True,
        delete_merged_branches: bool = True,
        exempt_labels: list[str] | None = None,
    ) -> None:
        self._github = github
        self._owner = owner
        self._repo = repo
        self._stale_days = stale_days
        self._close_after = close_after
        self._close_stale_prs = close_stale_prs
        self._delete_merged_branches = delete_merged_branches
        self._exempt_labels = _STALE_EXEMPT_LABELS | set(exempt_labels or [])

    async def run(self) -> StaleReport:
        report = StaleReport()
        now = datetime.now(UTC)

        # ── Issues ──────────────────────────────────────────────
        try:
            issues = await self._github.list_issues(self._owner, self._repo, state="open")
        except Exception as e:
            report.errors.append(f"list_issues: {e}")
            issues = []

        for issue in issues:
            if self._is_exempt(issue):
                continue
            try:
                updated_at = _parse_dt(
                    getattr(issue, "updated_at", None)
                    or getattr(issue, "raw", {}).get("updated_at")
                )
                if updated_at is None:
                    continue
                age = (now - updated_at).days
                label_names = {lbl.name for lbl in getattr(issue, "labels", [])}
                already_stale = STALE_LABEL in label_names

                if not already_stale and age >= self._stale_days:
                    # Warn — add stale label + comment
                    await self._github.add_labels(
                        self._owner, self._repo, issue.number, [STALE_LABEL]
                    )
                    await self._github.add_issue_comment(
                        self._owner,
                        self._repo,
                        issue.number,
                        _STALE_WARNING.format(days=age, close_in=self._close_after),
                    )
                    report.issues_warned += 1

                elif already_stale and age >= (self._stale_days + self._close_after):
                    await self._github.update_issue(
                        self._owner,
                        self._repo,
                        issue.number,
                        state="closed",
                        state_reason="not_planned",
                    )
                    report.issues_closed += 1
            except Exception as e:
                logger.warning("Stale agent: issue #%d error: %s", issue.number, e)
                report.errors.append(str(e))

        # ── Stale PRs ────────────────────────────────────────────
        if self._close_stale_prs:
            try:
                prs = await self._github.list_pull_requests(self._owner, self._repo, state="open")
            except Exception as e:
                report.errors.append(f"list_prs: {e}")
                prs = []

            for pr in prs:
                if pr.draft:
                    continue  # skip drafts — they signal WIP
                label_names = {lbl.name for lbl in getattr(pr, "labels", [])}
                if label_names & self._exempt_labels:
                    continue
                try:
                    updated_at = _parse_dt(
                        getattr(pr, "updated_at", None) or getattr(pr, "raw", {}).get("updated_at")
                    )
                    if updated_at is None:
                        continue
                    age = (now - updated_at).days
                    if age >= (self._stale_days + self._close_after):
                        await self._github.update_issue(
                            self._owner,
                            self._repo,
                            pr.number,
                            state="closed",
                        )
                        report.prs_closed += 1
                        logger.info("Stale agent: closed stale PR #%d", pr.number)
                except Exception as e:
                    logger.warning("Stale agent: PR #%d error: %s", pr.number, e)
                    report.errors.append(str(e))

        # ── Delete merged branches ───────────────────────────────
        if self._delete_merged_branches:
            try:
                branches_deleted = await self._prune_merged_branches()
                report.branches_deleted = branches_deleted
            except Exception as e:
                logger.warning("Stale agent: branch pruning error: %s", e)
                report.errors.append(str(e))

        logger.info(
            "Stale agent: warned=%d closed_issues=%d closed_prs=%d branches_deleted=%d",
            report.issues_warned,
            report.issues_closed,
            report.prs_closed,
            report.branches_deleted,
        )
        return report

    def _is_exempt(self, issue: object) -> bool:
        label_names = {lbl.name for lbl in getattr(issue, "labels", [])}
        return bool(label_names & self._exempt_labels)

    async def _prune_merged_branches(self) -> int:
        """Delete branches whose associated PRs have been merged."""
        merged_prs = await self._github.list_pull_requests(self._owner, self._repo, state="closed")
        deleted = 0
        for pr in merged_prs:
            if not getattr(pr, "merged", False):
                continue
            head_ref = getattr(pr, "head_ref", "")
            if not head_ref or head_ref in ("main", "master", "develop", "release"):
                continue
            try:
                await self._github.delete_branch(self._owner, self._repo, head_ref)
                deleted += 1
                logger.debug("Stale agent: deleted merged branch %s", head_ref)
            except Exception:
                pass  # branch may already be gone
        return deleted


def _parse_dt(value: str | datetime | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
