"""Upgrade Agent — checks for new releases and creates upgrade issues."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

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
            else:
                logger.info("Already on latest version %s", self._current_version)

        except Exception as e:
            logger.error("Upgrade check failed: %s", e)
            report.errors.append(str(e))

        return report
