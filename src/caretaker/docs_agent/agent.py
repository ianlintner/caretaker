"""Docs Agent — reconciles merged PRs against documentation and opens update PRs."""

from __future__ import annotations

import base64
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from caretaker.github_client.api import GitHubAPIError
from caretaker.tools.github import GitHubIssueTools, GitHubPullRequestTools

if TYPE_CHECKING:
    from caretaker.github_client.api import GitHubClient
    from caretaker.github_client.models import PullRequest

logger = logging.getLogger(__name__)

DOCS_LABEL = "documentation"
DOCS_AGENT_MARKER = "<!-- caretaker:docs-agent"

# Files the agent watches for documentation coverage
_DEFAULT_DOC_FILES = ["README.md", "CHANGELOG.md", "docs/"]


@dataclass
class DocsReport:
    """Results from a single Docs agent run."""

    prs_analyzed: int = 0
    changelog_updated: bool = False
    doc_pr_opened: int | None = None  # PR number if a docs-update PR was created
    errors: list[str] = field(default_factory=list)


class DocsAgent:
    """
    Weekly documentation reconciliation:
    1. Finds merged PRs since the last docs-agent run.
    2. Generates a CHANGELOG entry for the current week from the PR titles/bodies.
    3. Opens a pull request updating CHANGELOG.md (and optionally README.md).
    4. Posts a follow-up PR comment via the PAT-backed identity so @copilot can
       review / merge from a write-capable user mention.
    """

    def __init__(
        self,
        github: GitHubClient,
        owner: str,
        repo: str,
        default_branch: str = "main",
        lookback_days: int = 7,
        changelog_path: str = "CHANGELOG.md",
        update_readme: bool = False,
    ) -> None:
        self._github = github
        self._owner = owner
        self._repo = repo
        self._default_branch = default_branch
        self._lookback_days = lookback_days
        self._changelog_path = changelog_path
        self._update_readme = update_readme
        self._issues = GitHubIssueTools(github, owner, repo)
        self._pull_requests = GitHubPullRequestTools(github, owner, repo)

    async def run(self) -> DocsReport:
        report = DocsReport()

        since = datetime.now(UTC) - timedelta(days=self._lookback_days)

        # Find merged PRs in the lookback window
        try:
            merged_prs = await self._get_recently_merged_prs(since)
        except Exception as e:
            report.errors.append(f"list_prs: {e}")
            return report

        report.prs_analyzed = len(merged_prs)
        logger.info(
            "Docs agent: %d merged PR(s) in last %d days",
            len(merged_prs),
            self._lookback_days,
        )

        if not merged_prs:
            logger.info("Docs agent: no recent merged PRs — nothing to document")
            return report

        # Check if a docs-update PR is already open (dedup)
        open_docs_prs = await self._find_open_docs_prs()
        if open_docs_prs:
            logger.info(
                "Docs agent: open docs-update PR already exists (#%d) — skipping",
                open_docs_prs[0],
            )
            report.doc_pr_opened = open_docs_prs[0]
            return report

        # Build changelog entry
        changelog_entry = _build_changelog_entry(merged_prs)

        # Get current CHANGELOG.md contents
        try:
            current_content, current_sha = await self._get_file(self._changelog_path)
        except Exception as e:
            logger.warning("Docs agent: could not read %s: %s", self._changelog_path, e)
            current_content = ""
            current_sha = None

        new_content = _prepend_changelog_entry(current_content, changelog_entry)
        if new_content == current_content:
            logger.info("Docs agent: changelog already up to date")
            return report

        # Create a branch and commit the update
        week_str = datetime.now(UTC).strftime("%Y-W%V")
        branch_name = f"docs/changelog-{week_str}"

        try:
            # Get the SHA of the default branch tip
            base_sha = await self._get_branch_sha(self._default_branch)
            try:
                await self._github.create_branch(self._owner, self._repo, branch_name, base_sha)
            except GitHubAPIError as branch_err:
                if branch_err.status_code != 422:
                    raise
                # Branch already exists from a previous (possibly incomplete) run — reuse it.
                logger.warning("Docs agent: branch %r already exists — reusing it", branch_name)
                # Re-read the file SHA from the existing branch to avoid SHA mismatch on update.
                try:
                    _, current_sha = await self._get_file(self._changelog_path, ref=branch_name)
                except Exception as read_err:
                    logger.warning(
                        "Docs agent: could not read %s from branch %r (using default SHA): %s",
                        self._changelog_path,
                        branch_name,
                        read_err,
                    )
            await self._github.create_or_update_file(
                owner=self._owner,
                repo=self._repo,
                path=self._changelog_path,
                message=f"docs: update CHANGELOG for {week_str}",
                content=new_content,
                branch=branch_name,
                sha=current_sha,
            )

            await self._issues.ensure_label(
                DOCS_LABEL,
                color="0075ca",
                description="Documentation updates",
            )

            pr_body = _build_pr_body(merged_prs, changelog_entry)
            pr = await self._pull_requests.create(
                title=f"docs: reconcile CHANGELOG — {week_str}",
                body=pr_body,
                head=branch_name,
                base=self._default_branch,
                labels=[DOCS_LABEL],
                assignees=["copilot"],
            )
            pr_number = pr["number"] if isinstance(pr, dict) else pr.number
            await self._pull_requests.comment(
                pr_number,
                _build_copilot_review_comment(),
                use_copilot_token=True,
            )
            report.doc_pr_opened = pr_number
            report.changelog_updated = True
            logger.info("Docs agent: opened docs-update PR #%d", pr_number)
        except GitHubAPIError as e:
            if e.status_code == 403:
                # GitHub Actions is not permitted to create PRs in this repo —
                # this is a configuration/permission issue, not a caretaker bug.
                # Log as a warning so the run does not fail with exit code 1.
                logger.warning(
                    "Docs agent: skipping docs PR — insufficient permissions (403): %s", e
                )
            else:
                logger.error("Docs agent: failed to create docs PR: %s", e)
                report.errors.append(str(e))
        except Exception as e:
            logger.error("Docs agent: failed to create docs PR: %s", e)
            report.errors.append(str(e))

        return report

    async def _get_recently_merged_prs(self, since: datetime) -> list[PullRequest]:
        prs = await self._github.list_pull_requests(self._owner, self._repo, state="closed")
        merged = []
        for pr in prs:
            merged_at_str = getattr(pr, "merged_at", None) or (
                pr.raw.get("merged_at") if hasattr(pr, "raw") else None
            )
            if not merged_at_str:
                continue
            if isinstance(merged_at_str, str):
                merged_at = datetime.fromisoformat(merged_at_str.replace("Z", "+00:00"))
            else:
                merged_at = merged_at_str
            if merged_at >= since:
                merged.append(pr)
        return merged

    async def _find_open_docs_prs(self) -> list[int]:
        prs = await self._pull_requests.list(state="open")
        return [pr.number for pr in prs if (pr.body or "").find(DOCS_AGENT_MARKER) != -1]

    async def _get_file(self, path: str, ref: str | None = None) -> tuple[str, str | None]:
        data = await self._github.get_file_contents(self._owner, self._repo, path, ref=ref)
        if not data:
            return "", None
        content = base64.b64decode(data.get("content", "")).decode("utf-8")
        sha = data.get("sha")
        return content, sha

    async def _get_branch_sha(self, branch: str) -> str:
        data = await self._github._get(f"/repos/{self._owner}/{self._repo}/git/ref/heads/{branch}")
        return str(data["object"]["sha"])


def _build_changelog_entry(prs: list[Any]) -> str:
    week = datetime.now(UTC).strftime("%Y-W%V")
    date = datetime.now(UTC).strftime("%Y-%m-%d")
    lines = [f"## [{week}] — {date}\n"]
    for pr in sorted(prs, key=lambda p: p.number):
        title = _clean_title(pr.title)
        lines.append(f"- {title} (#{pr.number})")
    return "\n".join(lines) + "\n"


def _clean_title(title: str) -> str:
    """Strip conventional-commit prefix for readability."""
    _prefix_re = re.compile(
        r"^(feat|fix|chore|docs|refactor|test|ci|style|perf|build|revert)([!(/].*?)?:\s*"
    )
    return _prefix_re.sub("", title)


def _prepend_changelog_entry(current: str, entry: str) -> str:
    """Insert the new entry after the top-level # heading (if any), otherwise prepend."""
    if "\n## " in current:
        pos = current.index("\n## ")
        return current[:pos] + "\n" + entry + "\n" + current[pos + 1 :]
    # First entry ever
    if current.startswith("# "):
        newline_pos = current.index("\n")
        return current[: newline_pos + 1] + "\n" + entry + "\n" + current[newline_pos + 1 :]
    return entry + "\n" + current


def _build_pr_body(prs: list[Any], changelog_entry: str) -> str:
    pr_list = "\n".join(f"- #{pr.number}: {pr.title}" for pr in prs)
    return f"""## Documentation reconciliation

This PR updates `CHANGELOG.md` to capture the following merged pull requests:

{pr_list}

### Proposed changelog entry

```markdown
{changelog_entry}
```

---
{DOCS_AGENT_MARKER} -->"""


def _build_copilot_review_comment() -> str:
    return """@copilot Please review this generated documentation update for accuracy,
expand any cryptic titles if needed, and merge when ready.

<!-- caretaker:docs-review -->"""
