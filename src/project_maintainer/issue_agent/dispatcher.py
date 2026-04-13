"""Issue dispatcher — creates structured Copilot assignments."""

from __future__ import annotations

import logging

from project_maintainer.github_client.api import GitHubClient
from project_maintainer.github_client.models import Issue
from project_maintainer.issue_agent.classifier import IssueClassification

logger = logging.getLogger(__name__)


def build_assignment_body(issue: Issue, classification: IssueClassification) -> str:
    """Build a structured assignment body for Copilot."""
    lines = [
        f"## [Maintainer] Fix: {issue.title}",
        "",
        f"Fixes #{issue.number} (reported by @{issue.user.login})",
        "",
        "@copilot Please implement this fix. "
        "See `.github/agents/maintainer-issue.md` for your workflow.",
        "",
        "<!-- project-maintainer:assignment -->",
        f"TYPE: {classification.value}",
        f"SOURCE_ISSUE: #{issue.number}",
        "PRIORITY: medium",
        "",
        "**Original issue:**",
        issue.body or "(no body)",
        "",
        "**Acceptance criteria:**",
        "- [ ] The reported issue is resolved",
        "- [ ] Tests added for the fix",
        "- [ ] All existing tests continue to pass",
        "<!-- /project-maintainer:assignment -->",
    ]
    return "\n".join(lines)


class IssueDispatcher:
    """Creates structured issue assignments for Copilot."""

    def __init__(self, github: GitHubClient, owner: str, repo: str) -> None:
        self._github = github
        self._owner = owner
        self._repo = repo

    async def dispatch(
        self,
        issue: Issue,
        classification: IssueClassification,
    ) -> Issue | None:
        """Create a structured assignment issue for Copilot."""
        if classification in (
            IssueClassification.MAINTAINER_INTERNAL,
            IssueClassification.STALE,
            IssueClassification.DUPLICATE,
            IssueClassification.INFRA_OR_CONFIG,
        ):
            logger.info("Issue #%d (%s) not dispatchable", issue.number, classification.value)
            return None

        body = build_assignment_body(issue, classification)

        # For simple bugs, just update the existing issue and assign to Copilot
        if classification == IssueClassification.BUG_SIMPLE:
            comment_body = (
                f"@copilot This issue has been triaged as `{classification.value}`. "
                "Please fix it.\n\n"
                "See `.github/agents/maintainer-issue.md` for your workflow.\n\n"
                "<!-- project-maintainer:assignment -->\n"
                f"TYPE: {classification.value}\n"
                "PRIORITY: medium\n"
                "<!-- /project-maintainer:assignment -->"
            )
            await self._github.add_issue_comment(
                self._owner, self._repo, issue.number, comment_body
            )
            await self._github.update_issue(
                self._owner,
                self._repo,
                issue.number,
                assignees=["copilot"],
                labels=[l.name for l in issue.labels] + ["maintainer:assigned"],
            )
            logger.info("Issue #%d assigned to Copilot as %s", issue.number, classification.value)
            return issue

        # For features, create a new structured issue
        if classification in (
            IssueClassification.FEATURE_SMALL,
            IssueClassification.BUG_COMPLEX,
        ):
            new_issue = await self._github.create_issue(
                self._owner,
                self._repo,
                title=f"[Maintainer] {issue.title}",
                body=body,
                labels=["maintainer:internal", "maintainer:assigned"],
                assignees=["copilot"],
            )
            # Link back to original
            await self._github.add_issue_comment(
                self._owner,
                self._repo,
                issue.number,
                f"This issue has been picked up by the project-maintainer. "
                f"Tracking in #{new_issue.number}.",
            )
            logger.info(
                "Created assignment issue #%d for source issue #%d",
                new_issue.number,
                issue.number,
            )
            return new_issue

        return None
