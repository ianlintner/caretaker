"""Upgrade planner — generates upgrade issues for Copilot."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from caretaker.github_client.api import GitHubClient
    from caretaker.upgrade_agent.release_checker import Release

logger = logging.getLogger(__name__)


def build_upgrade_issue_body(
    current_version: str,
    target: Release,
) -> str:
    """Build the body for an upgrade issue."""
    lines = [
        f"## [Maintainer] Upgrade to v{target.version}",
        "",
        f"Current version: `{current_version}`",
        f"Target version: `{target.version}`",
        "",
    ]

    if target.breaking:
        lines.extend(
            [
                "⚠️ **This is a breaking release.** Manual review is recommended.",
                "",
            ]
        )

    if target.upgrade_notes:
        lines.extend(
            [
                "### Upgrade Notes",
                target.upgrade_notes,
                "",
            ]
        )

    if target.changelog_url:
        lines.extend(
            [
                f"📋 [Full Changelog]({target.changelog_url})",
                "",
            ]
        )

    lines.extend(
        [
            "@copilot Please apply this upgrade.",
            "See `.github/agents/maintainer-upgrade.md` for instructions.",
            "",
            "<!-- caretaker:upgrade -->",
            f"FROM: {current_version}",
            f"TO: {target.version}",
            f"BREAKING: {target.breaking}",
            "<!-- /caretaker:upgrade -->",
            "",
            "**Steps:**",
            "1. Update version pins in `pyproject.toml` / `requirements.txt`",
            "2. Update any workflow references",
            "3. Run tests to verify compatibility",
            "4. Update the version in config if applicable",
            "",
            "**Acceptance criteria:**",
            "- [ ] Version updated to target",
            "- [ ] All tests pass",
            "- [ ] No regressions",
        ]
    )

    return "\n".join(lines)


class UpgradePlanner:
    """Creates upgrade issues for pending releases."""

    def __init__(self, github: GitHubClient, owner: str, repo: str) -> None:
        self._github = github
        self._owner = owner
        self._repo = repo

    async def create_upgrade_issue(
        self,
        current_version: str,
        target: Release,
    ) -> int:
        """Create an upgrade issue and return its number."""
        # Check if an upgrade issue already exists for this version
        issues = await self._github.list_issues(self._owner, self._repo)
        for issue in issues:
            if f"Upgrade to v{target.version}" in issue.title and issue.is_maintainer_issue:
                logger.info(
                    "Upgrade issue for v%s already exists: #%d",
                    target.version,
                    issue.number,
                )
                return issue.number

        body = build_upgrade_issue_body(current_version, target)
        labels = ["maintainer:internal", "upgrade"]
        if target.breaking:
            labels.append("breaking")

        issue = await self._github.create_issue(
            self._owner,
            self._repo,
            title=f"[Maintainer] Upgrade to v{target.version}",
            body=body,
            labels=labels,
            assignees=["copilot"] if not target.breaking else [],
        )
        logger.info("Created upgrade issue #%d for v%s", issue.number, target.version)
        return issue.number
