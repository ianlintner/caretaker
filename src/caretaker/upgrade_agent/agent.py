"""Upgrade Agent — checks for new releases and creates upgrade issues."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from caretaker.pr_agent.pr_triage import ready_valid_copilot_drafts
from caretaker.upgrade_agent.planner import UpgradePlanner
from caretaker.upgrade_agent.release_checker import fetch_releases, needs_upgrade

if TYPE_CHECKING:
    from caretaker.config import UpgradeAgentConfig
    from caretaker.foundry.dispatcher import ExecutorDispatcher
    from caretaker.github_client.api import GitHubClient

logger = logging.getLogger(__name__)


@dataclass
class UpgradeAgentReport:
    checked: bool = False
    latest_version: str | None = None
    upgrade_needed: bool = False
    upgrade_issue: int | None = None
    superseded_prs: list[int] = field(default_factory=list)
    readied_draft_prs: list[int] = field(default_factory=list)
    closed_stale_upgrade_issues: list[int] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class UpgradeAgent:
    """Checks for new caretaker releases and creates upgrade issues."""

    def __init__(
        self,
        github: GitHubClient,
        owner: str,
        repo: str,
        config: UpgradeAgentConfig,
        current_version: str,
        dispatcher: ExecutorDispatcher | None = None,
    ) -> None:
        self._github = github
        self._owner = owner
        self._repo = repo
        self._config = config
        self._current_version = current_version
        self._planner = UpgradePlanner(github, owner, repo, dispatcher=dispatcher)

    async def run(self) -> UpgradeAgentReport:
        """Check for upgrades and create issues if needed."""
        report = UpgradeAgentReport()

        if not self._config.enabled:
            logger.info("Upgrade agent is disabled")
            return report

        try:
            releases = await fetch_releases()
            report.checked = True

            if not releases:
                logger.info("No releases found")
                return report

            latest = releases[0]
            report.latest_version = latest.version

            if needs_upgrade(self._current_version, latest):
                report.upgrade_needed = True
                issue_number = await self._planner.create_upgrade_issue(
                    self._current_version, latest
                )
                report.upgrade_issue = issue_number
                logger.info(
                    "Upgrade from %s to %s — issue #%d",
                    self._current_version,
                    latest.version,
                    issue_number,
                )
                report.superseded_prs = await self._planner.close_superseded_upgrade_prs(
                    latest.version
                )
            else:
                logger.info("Already on latest version %s", self._current_version)
                # Even when no upgrade is needed, sweep stale upgrade
                # issues whose targets the consumer has already moved past.
                # Otherwise every "[Maintainer] Upgrade to vX.Y.Z" issue
                # stays OPEN forever once the pin is bumped past it.
                try:
                    closed = await self._planner.close_stale_upgrade_issues(self._current_version)
                    report.closed_stale_upgrade_issues = closed
                    if closed:
                        logger.info(
                            "Closed %d stale upgrade issue(s): %s",
                            len(closed),
                            closed,
                        )
                except Exception as e:
                    logger.warning("Stale upgrade issue cleanup failed: %s", e)

        except Exception as e:
            logger.error("Upgrade check failed: %s", e)
            report.errors.append(str(e))

        # Promote our own draft upgrade PRs once CI passes, regardless of
        # whether an upgrade was needed this run.  This closes the loop where
        # Copilot opens upgrade PRs as drafts and they stall forever because
        # pr_ci_approver isn't enabled on the consumer repo.
        if self._config.auto_ready_drafts:
            try:
                open_prs = await self._github.list_pull_requests(
                    self._owner, self._repo, state="open"
                )
                report.readied_draft_prs = await ready_valid_copilot_drafts(
                    self._github, self._owner, self._repo, open_prs
                )
                if report.readied_draft_prs:
                    logger.info(
                        "auto_ready_drafts: promoted %d draft PR(s) to ready-for-review: %s",
                        len(report.readied_draft_prs),
                        report.readied_draft_prs,
                    )
            except Exception as e:
                logger.warning("auto_ready_drafts check failed: %s", e)

        return report
