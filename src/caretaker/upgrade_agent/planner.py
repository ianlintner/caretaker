"""Upgrade planner — generates upgrade issues for Copilot."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from caretaker.tools.github import GitHubIssueTools

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


_REPO_BASE = "https://raw.githubusercontent.com/ianlintner/caretaker"

# Files that must stay in sync with the installed caretaker version.
SYNC_FILES: list[tuple[str, str]] = [
    (
        ".github/workflows/maintainer.yml",
        "setup-templates/templates/workflows/maintainer.yml",
    ),
    (
        ".github/agents/maintainer-pr.md",
        "setup-templates/templates/agents/maintainer-pr.md",
    ),
    (
        ".github/agents/maintainer-issue.md",
        "setup-templates/templates/agents/maintainer-issue.md",
    ),
    (
        ".github/agents/maintainer-upgrade.md",
        "setup-templates/templates/agents/maintainer-upgrade.md",
    ),
    (
        ".github/maintainer/config.yml",
        "setup-templates/templates/config-default.yml",
    ),
]


def build_sync_issue_body(version: str) -> str:
    """Build the body for a workflow/file sync issue.

    This is used when a client repo has the correct version pinned but its
    workflow files, agent templates, or config may be out of date.
    """
    tag_ref = f"v{version}"
    lines = [
        f"## [Maintainer] Sync installation files to v{version}",
        "",
        f"Installed version: `{version}`",
        "",
        "The caretaker version file (`.github/maintainer/.version`) indicates "
        f"`{version}`, but one or more supporting files may be out of date. "
        "Please reconcile every file listed below so that the installation is "
        "fully in line with the running version.",
        "",
        "@copilot Please sync the files listed below.",
        "See `.github/agents/maintainer-upgrade.md` for general guidance.",
        "",
        "<!-- caretaker:sync -->",
        f"VERSION: {version}",
        "<!-- /caretaker:sync -->",
        "",
        "### Files to sync",
        "",
        "For each file, fetch the canonical template from the caretaker repo at "
        f"**tag `{tag_ref}`** and replace the local copy. If the local file "
        "does not exist, create it.",
        "",
    ]

    for local_path, template_path in SYNC_FILES:
        url = f"{_REPO_BASE}/{tag_ref}/{template_path}"
        lines.append(f"- **`{local_path}`**")
        lines.append(f"  Source: {url}")
        lines.append("")

    lines.extend(
        [
            "### Version file",
            "",
            f"Ensure `.github/maintainer/.version` contains exactly `{version}` "
            "(no `v` prefix, no trailing whitespace).",
            "",
            "### Copilot instructions",
            "",
            "If `.github/copilot-instructions.md` does not already contain a "
            "`## Caretaker` section, append the standard block from the "
            "[setup guide]"
            f"({_REPO_BASE}/{tag_ref}/dist/SETUP_AGENT.md).",
            "",
            "**Steps:**",
            "1. Fetch each template from the URLs above and overwrite the local copy",
            "2. Verify `.github/maintainer/.version` matches the installed version",
            "3. Ensure `.github/copilot-instructions.md` has the Caretaker section",
            "4. Run the existing test suite to confirm nothing breaks",
            "5. Open a PR with all changes",
            "",
            "**Acceptance criteria:**",
            "- [ ] All listed files match the canonical templates",
            f"- [ ] `.github/maintainer/.version` is `{version}`",
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
        self._issues = GitHubIssueTools(github, owner, repo)

    async def create_upgrade_issue(
        self,
        current_version: str,
        target: Release,
    ) -> int:
        """Create an upgrade issue and return its number."""
        # Check if an upgrade issue already exists for this version (open or closed).
        # Using state="all" prevents re-creating the issue when a previous attempt was
        # closed without completing the upgrade, which would spawn duplicate Copilot PRs.
        issues = await self._issues.list(state="all")
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

        issue = await self._issues.create(
            title=f"[Maintainer] Upgrade to v{target.version}",
            body=body,
            labels=labels,
            assignees=["copilot"] if not target.breaking else [],
            copilot_assignment=self._issues.default_copilot_assignment(),
        )
        logger.info("Created upgrade issue #%d for v%s", issue.number, target.version)
        return issue.number

    async def create_sync_issue(self, version: str) -> int:
        """Create a sync issue for the given version and return its number.

        A sync issue tells the client agent to reconcile all workflow files,
        agent templates, and config against the canonical templates for
        *version*.  If a matching open sync issue already exists it is reused.
        """
        issues = await self._issues.list()
        for issue in issues:
            if (
                f"Sync installation files to v{version}" in issue.title
                and issue.is_maintainer_issue
            ):
                logger.info(
                    "Sync issue for v%s already exists: #%d",
                    version,
                    issue.number,
                )
                return issue.number

        body = build_sync_issue_body(version)
        issue = await self._issues.create(
            title=f"[Maintainer] Sync installation files to v{version}",
            body=body,
            labels=["maintainer:internal", "sync"],
            assignees=["copilot"],
            copilot_assignment=self._issues.default_copilot_assignment(),
        )
        logger.info("Created sync issue #%d for v%s", issue.number, version)
        return issue.number
