"""Upgrade planner — generates upgrade issues for Copilot."""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from caretaker.causal import make_causal_marker
from caretaker.tools.github import GitHubIssueTools

_UPGRADE_MARKER_RE = re.compile(r"<!--\s*caretaker:upgrade target=([^\s>]+)\s*-->")

if TYPE_CHECKING:
    from caretaker.foundry.dispatcher import ExecutorDispatcher
    from caretaker.github_client.api import GitHubClient
    from caretaker.github_client.models import PullRequest
    from caretaker.upgrade_agent.release_checker import Release

logger = logging.getLogger(__name__)


def _upgrade_target_marker(version: str) -> str:
    """HTML-comment marker that uniquely identifies an upgrade issue by version.

    Used for robust dedupe instead of title-substring matching, which
    historically allowed multiple issues for the same target version
    (rust-oauth2-server #118/#121/#126/#129/#153 all targeted v0.5.0).
    """
    return f"<!-- caretaker:upgrade target={version} -->"


def _sync_target_marker(version: str) -> str:
    return f"<!-- caretaker:sync target={version} -->"


def build_upgrade_issue_body(
    current_version: str,
    target: Release,
) -> str:
    """Build the body for an upgrade issue."""
    lines = [
        _upgrade_target_marker(target.version),
        make_causal_marker("upgrade"),
        "",
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
            f"({_REPO_BASE}/{tag_ref}/setup-templates/SETUP_AGENT.md).",
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
    """Creates upgrade issues for pending releases.

    When a Foundry :class:`ExecutorDispatcher` is provided, the planner
    *records* the dispatcher but does not change its issue-creation flow in
    MVP — the dispatcher is used by the caretaker orchestrator's higher-level
    upgrade-PR path.  Keeping the field lets that integration land without
    a signature change.
    """

    def __init__(
        self,
        github: GitHubClient,
        owner: str,
        repo: str,
        dispatcher: ExecutorDispatcher | None = None,
    ) -> None:
        self._github = github
        self._owner = owner
        self._repo = repo
        self._issues = GitHubIssueTools(github, owner, repo)
        # TODO(foundry-phase-2): route non-breaking upgrade tasks through
        # this dispatcher instead of creating @copilot-assigned issues.
        self._dispatcher = dispatcher

    @property
    def dispatcher(self) -> ExecutorDispatcher | None:
        return self._dispatcher

    async def create_upgrade_issue(
        self,
        current_version: str,
        target: Release,
    ) -> int:
        """Create an upgrade issue and return its number.

        Dedupe is body-marker first, title-substring second:

        1. Look for ``<!-- caretaker:upgrade target=X.Y.Z -->`` in any issue
           body (open or closed) — this is the precise key.
        2. Fall back to the legacy title-substring check for issues created
           before the marker was added; backfill the marker so the next
           lookup is exact.
        3. Otherwise create a new issue with the marker baked in.

        The two-step lookup closes the rust-oauth2-server #118/#121/#126/
        #129/#153 pattern where five issues all targeted v0.5.0.
        """
        target_marker = _upgrade_target_marker(target.version)
        legacy_title_substring = f"Upgrade to v{target.version}"

        issues = await self._issues.list(state="all")

        # Step 1: precise marker-based dedupe
        for issue in issues:
            if target_marker in (issue.body or ""):
                logger.info(
                    "Upgrade issue for v%s already exists (marker): #%d",
                    target.version,
                    issue.number,
                )
                return issue.number

        # Step 2: legacy title-substring fallback + backfill marker.
        # Skip issues that already carry a marker for a *different* target,
        # since the marker is authoritative even if the title is misleading.
        for issue in issues:
            if legacy_title_substring not in issue.title or not issue.is_maintainer_issue:
                continue
            existing_marker_match = _UPGRADE_MARKER_RE.search(issue.body or "")
            if existing_marker_match and existing_marker_match.group(1) != target.version:
                continue  # belongs to a different upgrade target
            logger.info(
                "Upgrade issue for v%s already exists (legacy title): #%d — backfilling marker",
                target.version,
                issue.number,
            )
            try:
                new_body = (issue.body or "").rstrip() + f"\n\n{target_marker}\n"
                await self._issues.update(issue.number, body=new_body)
            except Exception as e:  # backfill is best-effort
                logger.warning(
                    "Failed to backfill upgrade marker on issue #%d: %s",
                    issue.number,
                    e,
                )
            return issue.number

        # Step 3: close any open upgrade issues for older versions.
        # Keeps at most one open upgrade issue per repo — resolves the
        # fleet-wide pile-up described in issue #510.
        for issue in issues:
            if issue.state != "open":
                continue
            older_match = _UPGRADE_MARKER_RE.search(issue.body or "")
            if not older_match or older_match.group(1) == target.version:
                continue
            logger.info(
                "Closing superseded upgrade issue #%d (v%s → v%s)",
                issue.number,
                older_match.group(1),
                target.version,
            )
            try:
                await self._issues.comment(
                    issue.number,
                    f"Superseded by newer upgrade target v{target.version} — "
                    "closing to keep only one open upgrade issue per repo.",
                )
                await self._issues.update(issue.number, state="closed")
            except Exception as e:
                logger.warning("Failed to close superseded upgrade issue #%d: %s", issue.number, e)

        # Step 4: no existing issue — create one with marker in body
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

    async def close_superseded_upgrade_prs(self, version: str) -> list[int]:
        """Close older open upgrade PRs for the same target version.

        When two workflow runs open upgrade PRs in parallel (racing on the
        same upgrade issue), the older one is superseded. Keeps the
        highest-numbered open PR whose title references ``Upgrade to
        v{version}``; closes the rest with a ``Superseded by #N`` comment.

        Returns the list of PR numbers closed.
        """
        from caretaker.dedupe import close_superseded_prs

        title_substring = f"Upgrade to v{version}"
        try:
            prs = await self._github.list_pull_requests(self._owner, self._repo, state="open")
        except Exception as e:
            logger.warning("Failed to list open PRs for upgrade dedupe: %s", e)
            return []

        def _key(pr: PullRequest) -> str | None:
            return f"upgrade:{version}" if title_substring in (pr.title or "") else None

        def _comment(closed: PullRequest, keeper: PullRequest) -> str:
            return f"Superseded by #{keeper.number} (both target v{version})."

        return await close_superseded_prs(
            self._github,
            self._owner,
            self._repo,
            prs,
            bucket_key=_key,
            comment=_comment,
        )

    async def create_sync_issue(self, version: str) -> int:
        """Create a sync issue for the given version and return its number.

        A sync issue tells the client agent to reconcile all workflow files,
        agent templates, and config against the canonical templates for
        *version*.  Dedupe uses the same marker-first / title-second pattern
        as :meth:`create_upgrade_issue`.
        """
        target_marker = _sync_target_marker(version)
        legacy_title_substring = f"Sync installation files to v{version}"

        issues = await self._issues.list()

        for issue in issues:
            if target_marker in (issue.body or ""):
                logger.info(
                    "Sync issue for v%s already exists (marker): #%d",
                    version,
                    issue.number,
                )
                return issue.number

        for issue in issues:
            if legacy_title_substring in issue.title and issue.is_maintainer_issue:
                logger.info(
                    "Sync issue for v%s already exists (legacy title): #%d — backfilling marker",
                    version,
                    issue.number,
                )
                try:
                    new_body = (issue.body or "").rstrip() + f"\n\n{target_marker}\n"
                    await self._issues.update(issue.number, body=new_body)
                except Exception as e:
                    logger.warning(
                        "Failed to backfill sync marker on issue #%d: %s",
                        issue.number,
                        e,
                    )
                return issue.number

        body = build_sync_issue_body(version) + f"\n\n{target_marker}\n"
        issue = await self._issues.create(
            title=f"[Maintainer] Sync installation files to v{version}",
            body=body,
            labels=["maintainer:internal", "sync"],
            assignees=["copilot"],
            copilot_assignment=self._issues.default_copilot_assignment(),
        )
        logger.info("Created sync issue #%d for v%s", issue.number, version)
        return issue.number
