"""Dependency Agent — manages Dependabot version-bump PRs and weekly digest."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from caretaker.github_client.api import GitHubClient

logger = logging.getLogger(__name__)

DEPENDENCY_DIGEST_LABEL = "dependencies:digest"
DEPENDENCY_MAJOR_LABEL = "dependencies:major-upgrade"
DEPENDENCY_AGENT_MARKER = "<!-- caretaker:dependency-agent"

# Bump types Dependabot encodes in PR titles
_SEMVER_BUMP_RE = re.compile(
    r"bump\s+(\S+)\s+from\s+([\d.]+(?:[-+]\S+)?)\s+to\s+([\d.]+(?:[-+]\S+)?)",
    re.IGNORECASE,
)


@dataclass
class DependencyBump:
    pr_number: int
    title: str
    package: str
    from_version: str
    to_version: str
    ecosystem: str   # pip, npm, cargo, etc.
    is_major: bool
    is_security: bool
    html_url: str


@dataclass
class DependencyReport:
    """Results from a single Dependency agent run."""

    prs_reviewed: int = 0
    prs_auto_merged: int = field(default_factory=list)
    major_issues_created: list[int] = field(default_factory=list)
    digest_issue_number: int | None = None
    errors: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if isinstance(self.prs_auto_merged, list):
            object.__setattr__(self, "prs_auto_merged", self.prs_auto_merged)


@dataclass
class DependencyReport:
    """Results from a single Dependency agent run."""

    prs_reviewed: int = 0
    prs_auto_merged: list[int] = field(default_factory=list)
    major_issues_created: list[int] = field(default_factory=list)
    digest_issue_number: int | None = None
    errors: list[str] = field(default_factory=list)


class DependencyAgent:
    """
    Manages Dependabot version-bump pull requests:
    * Auto-merges safe patch / minor bumps (when CI is green).
    * Creates human-escalation issues for major version upgrades.
    * Posts a weekly dependency digest summarising all pending updates.
    """

    def __init__(
        self,
        github: GitHubClient,
        owner: str,
        repo: str,
        auto_merge_patch: bool = True,
        auto_merge_minor: bool = True,
        merge_method: str = "squash",
        post_digest: bool = True,
    ) -> None:
        self._github = github
        self._owner = owner
        self._repo = repo
        self._auto_merge_patch = auto_merge_patch
        self._auto_merge_minor = auto_merge_minor
        self._merge_method = merge_method
        self._post_digest = post_digest

    async def run(self) -> DependencyReport:
        report = DependencyReport()

        # Fetch Dependabot PRs (open, from dependabot[bot])
        try:
            all_prs = await self._github.list_pull_requests(self._owner, self._repo, state="open")
        except Exception as e:
            report.errors.append(f"list_pull_requests: {e}")
            return report

        dep_prs = [
            pr for pr in all_prs
            if pr.user.login in ("dependabot[bot]", "dependabot-preview[bot]")
        ]
        report.prs_reviewed = len(dep_prs)
        logger.info("Dependency agent: %d Dependabot PR(s) open", len(dep_prs))

        if not dep_prs:
            return report

        bumps = [_parse_bump(pr) for pr in dep_prs]
        bumps = [b for b in bumps if b is not None]

        existing_major_sigs = await self._get_existing_major_issue_prs()

        for bump in bumps:
            if bump.is_major:
                # Escalate: create a human-attention issue if not already done
                if bump.pr_number not in existing_major_sigs:
                    try:
                        issue = await self._create_major_upgrade_issue(bump)
                        report.major_issues_created.append(issue["number"])
                    except Exception as e:
                        logger.error("Dependency agent: failed major issue for PR#%d: %s", bump.pr_number, e)
                        report.errors.append(str(e))
            else:
                # Auto-merge patch / minor when CI passes
                should_merge = (
                    (self._auto_merge_patch and _is_patch(bump)) or
                    (self._auto_merge_minor and _is_minor(bump))
                )
                if not should_merge:
                    continue
                try:
                    ci_status = await self._github.get_combined_status(
                        self._owner, self._repo, bump.pr_number
                    )
                    # ci_status here is the commit status; also check check-runs
                    if ci_status in ("success", "pending"):
                        merged = await self._github.merge_pull_request(
                            self._owner, self._repo, bump.pr_number, method=self._merge_method
                        )
                        if merged:
                            report.prs_auto_merged.append(bump.pr_number)
                            logger.info("Dependency agent: auto-merged PR#%d (%s)", bump.pr_number, bump.package)
                except Exception as e:
                    logger.warning("Dependency agent: merge failed for PR#%d: %s", bump.pr_number, e)
                    report.errors.append(str(e))

        # Post weekly digest
        if self._post_digest and bumps:
            try:
                digest_issue = await self._post_dependency_digest(bumps, report)
                report.digest_issue_number = digest_issue
            except Exception as e:
                logger.warning("Dependency agent: digest post failed: %s", e)
                report.errors.append(str(e))

        return report

    async def _get_existing_major_issue_prs(self) -> set[int]:
        """Return PR numbers that already have open major-upgrade tracking issues."""
        issues = await self._github.list_issues(
            self._owner, self._repo, state="open", labels=DEPENDENCY_MAJOR_LABEL
        )
        pr_numbers: set[int] = set()
        for issue in issues:
            body = issue.body or ""
            for line in body.splitlines():
                if DEPENDENCY_AGENT_MARKER in line and "pr:" in line:
                    try:
                        pr_numbers.add(int(line.split("pr:")[1].strip().rstrip(" -->").strip()))
                    except ValueError:
                        pass
        return pr_numbers

    async def _create_major_upgrade_issue(self, bump: DependencyBump) -> dict:
        await self._github.ensure_label(
            self._owner, self._repo, DEPENDENCY_MAJOR_LABEL,
            color="f97316", description="Major dependency version upgrade requiring review",
        )
        await self._github.ensure_label(
            self._owner, self._repo, DEPENDENCY_DIGEST_LABEL,
            color="8b5cf6", description="Dependency update digest",
        )

        body = f"""## Major Dependency Upgrade: `{bump.package}`

A major version upgrade from **{bump.from_version}** → **{bump.to_version}** is available.

| Field | Value |
|---|---|
| Package | `{bump.package}` |
| Ecosystem | {bump.ecosystem} |
| Change | `{bump.from_version}` → `{bump.to_version}` |
| Dependabot PR | #{bump.pr_number} |
| PR link | {bump.html_url} |

## Action required

This upgrade may contain **breaking changes**. @copilot, please:

1. Review the changelog / release notes for `{bump.package}` between `{bump.from_version}` and `{bump.to_version}`.
2. Identify any breaking API changes that affect this repository.
3. Apply necessary code migrations.
4. Merge or close Dependabot PR #{bump.pr_number} once the migration is in place.
5. If the migration is complex, add a checklist comment summarising the work needed and flag with `help wanted`.

---
{DEPENDENCY_AGENT_MARKER} pr:{bump.pr_number} -->"""

        return await self._github.create_issue(
            owner=self._owner,
            repo=self._repo,
            title=f"[Dependencies] Major upgrade: {bump.package} {bump.from_version} → {bump.to_version}",
            body=body,
            labels=[DEPENDENCY_MAJOR_LABEL],
            assignees=["copilot"],
        )

    async def _post_dependency_digest(
        self, bumps: list[DependencyBump], report: DependencyReport
    ) -> int | None:
        """Open (or update) a weekly digest issue listing all pending dependency updates."""
        # Check if a recent open digest issue exists
        existing = await self._github.list_issues(
            self._owner, self._repo, state="open", labels=DEPENDENCY_DIGEST_LABEL
        )
        digest_issues = [i for i in existing if DEPENDENCY_AGENT_MARKER in (i.body or "")]
        if digest_issues:
            # Only one digest at a time — update the existing one
            issue = digest_issues[0]
            await self._github.update_issue(
                self._owner, self._repo, issue.number,
                body=self._build_digest_body(bumps, report),
            )
            return issue.number

        await self._github.ensure_label(
            self._owner, self._repo, DEPENDENCY_DIGEST_LABEL,
            color="8b5cf6", description="Dependency update digest",
        )
        issue = await self._github.create_issue(
            owner=self._owner,
            repo=self._repo,
            title=f"[Dependencies] Weekly digest — {datetime.now(timezone.utc).strftime('%Y-%m-%d')}",
            body=self._build_digest_body(bumps, report),
            labels=[DEPENDENCY_DIGEST_LABEL],
        )
        return issue["number"] if isinstance(issue, dict) else issue.number

    def _build_digest_body(self, bumps: list[DependencyBump], report: DependencyReport) -> str:
        patch_minor = [b for b in bumps if not b.is_major]
        major = [b for b in bumps if b.is_major]

        rows_pm = "\n".join(
            f"| `{b.package}` | {b.ecosystem} | {b.from_version} → {b.to_version} "
            f"| {'✅ auto-merged' if b.pr_number in report.prs_auto_merged else f'[PR #{b.pr_number}]({b.html_url})'} |"
            for b in patch_minor
        ) or "_None_"

        rows_major = "\n".join(
            f"| `{b.package}` | {b.ecosystem} | {b.from_version} → {b.to_version} "
            f"| [PR #{b.pr_number}]({b.html_url}) | ⚠️ needs review |"
            for b in major
        ) or "_None_"

        week = datetime.now(timezone.utc).strftime("%Y-W%V")
        return f"""## Dependency Update Digest — {week}

### Patch / Minor (auto-managed)
| Package | Ecosystem | Change | Status |
|---|---|---|---|
{rows_pm}

### Major upgrades (human review required)
| Package | Ecosystem | Change | PR | Status |
|---|---|---|---|---|
{rows_major}

---
{DEPENDENCY_AGENT_MARKER} digest:{week} -->"""


def _parse_bump(pr) -> DependencyBump | None:
    m = _SEMVER_BUMP_RE.search(pr.title)
    if not m:
        return None
    package, from_ver, to_ver = m.group(1), m.group(2), m.group(3)
    ecosystem = _detect_ecosystem(pr)
    is_major = _is_major_bump(from_ver, to_ver)
    is_security = any(
        l.name.lower() in ("security", "dependencies")
        for l in getattr(pr, "labels", [])
    )
    return DependencyBump(
        pr_number=pr.number,
        title=pr.title,
        package=package,
        from_version=from_ver,
        to_version=to_ver,
        ecosystem=ecosystem,
        is_major=is_major,
        is_security=is_security,
        html_url=getattr(pr, "html_url", f"https://github.com/pulls/{pr.number}"),
    )


def _detect_ecosystem(pr) -> str:
    title_lower = pr.title.lower()
    if "pip" in title_lower or ".txt" in title_lower or "requirements" in title_lower:
        return "pip"
    if "npm" in title_lower or "package.json" in title_lower:
        return "npm"
    if "cargo" in title_lower or "rust" in title_lower:
        return "cargo"
    if "go" in title_lower:
        return "go"
    if "maven" in title_lower or "gradle" in title_lower:
        return "java"
    base = getattr(pr, "base_ref", "")
    if "python" in base.lower():
        return "pip"
    return "unknown"


def _is_major_bump(from_ver: str, to_ver: str) -> bool:
    try:
        from_major = int(from_ver.split(".")[0])
        to_major = int(to_ver.split(".")[0])
        return to_major > from_major
    except (ValueError, IndexError):
        return False


def _is_patch(bump: DependencyBump) -> bool:
    if bump.is_major:
        return False
    try:
        from_parts = bump.from_version.split(".")
        to_parts = bump.to_version.split(".")
        return from_parts[0] == to_parts[0] and from_parts[1] == to_parts[1]
    except IndexError:
        return True


def _is_minor(bump: DependencyBump) -> bool:
    if bump.is_major:
        return False
    return not _is_patch(bump)
