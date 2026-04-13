"""Issue Agent — triages, classifies, and dispatches issues."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from project_maintainer.config import IssueAgentConfig
from project_maintainer.github_client.api import GitHubClient
from project_maintainer.github_client.models import Issue
from project_maintainer.issue_agent.classifier import IssueClassification, classify_issue
from project_maintainer.issue_agent.dispatcher import IssueDispatcher
from project_maintainer.llm.router import LLMRouter
from project_maintainer.state.models import IssueTrackingState, TrackedIssue

logger = logging.getLogger(__name__)


@dataclass
class IssueAgentReport:
    triaged: int = 0
    assigned: list[int] = field(default_factory=list)
    closed: list[int] = field(default_factory=list)
    escalated: list[int] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class IssueAgent:
    """Triages and dispatches issues to Copilot."""

    def __init__(
        self,
        github: GitHubClient,
        owner: str,
        repo: str,
        config: IssueAgentConfig,
        llm_router: LLMRouter | None = None,
    ) -> None:
        self._github = github
        self._owner = owner
        self._repo = repo
        self._config = config
        self._llm = llm_router
        self._dispatcher = IssueDispatcher(github, owner, repo)

    async def run(
        self, tracked_issues: dict[int, TrackedIssue]
    ) -> tuple[IssueAgentReport, dict[int, TrackedIssue]]:
        """Run the issue agent — triage all open issues."""
        report = IssueAgentReport()

        issues = await self._github.list_issues(self._owner, self._repo)

        for issue in issues:
            try:
                tracking = tracked_issues.get(
                    issue.number, TrackedIssue(number=issue.number)
                )

                # Skip already-processed issues
                if tracking.state in (
                    IssueTrackingState.ASSIGNED,
                    IssueTrackingState.IN_PROGRESS,
                    IssueTrackingState.PR_OPENED,
                    IssueTrackingState.COMPLETED,
                    IssueTrackingState.ESCALATED,
                ):
                    continue

                classification = classify_issue(issue, self._config)
                tracking.classification = classification.value
                report.triaged += 1

                tracking = await self._process_issue(
                    issue, classification, tracking, report
                )
                tracking.last_checked = datetime.utcnow()
                tracked_issues[issue.number] = tracking

            except Exception as e:
                logger.error("Error processing issue #%d: %s", issue.number, e)
                report.errors.append(f"Issue #{issue.number}: {e}")

        return report, tracked_issues

    async def _process_issue(
        self,
        issue: Issue,
        classification: IssueClassification,
        tracking: TrackedIssue,
        report: IssueAgentReport,
    ) -> TrackedIssue:
        """Process a classified issue."""
        match classification:
            case IssueClassification.MAINTAINER_INTERNAL:
                pass  # Skip

            case IssueClassification.BUG_SIMPLE:
                if self._config.auto_assign_bugs:
                    result = await self._dispatcher.dispatch(issue, classification)
                    if result:
                        tracking.state = IssueTrackingState.ASSIGNED
                        report.assigned.append(issue.number)

            case IssueClassification.BUG_COMPLEX:
                result = await self._dispatcher.dispatch(issue, classification)
                if result:
                    tracking.state = IssueTrackingState.ASSIGNED
                    report.assigned.append(issue.number)

            case IssueClassification.FEATURE_SMALL:
                if self._config.auto_assign_features:
                    result = await self._dispatcher.dispatch(issue, classification)
                    if result:
                        tracking.state = IssueTrackingState.ASSIGNED
                        report.assigned.append(issue.number)
                else:
                    tracking.state = IssueTrackingState.TRIAGED

            case IssueClassification.FEATURE_LARGE:
                await self._escalate(issue, "Large feature — needs human decomposition")
                tracking.state = IssueTrackingState.ESCALATED
                report.escalated.append(issue.number)

            case IssueClassification.QUESTION:
                if self._config.auto_close_questions:
                    await self._github.add_issue_comment(
                        self._owner,
                        self._repo,
                        issue.number,
                        "This issue has been classified as a question. "
                        "Please check the project documentation and README for guidance. "
                        "If this needs further attention, please reopen.",
                    )
                    await self._github.update_issue(
                        self._owner, self._repo, issue.number, state="closed"
                    )
                    tracking.state = IssueTrackingState.CLOSED
                    report.closed.append(issue.number)

            case IssueClassification.INFRA_OR_CONFIG:
                await self._escalate(
                    issue, "Infrastructure/config issue — requires human access"
                )
                tracking.state = IssueTrackingState.ESCALATED
                report.escalated.append(issue.number)

            case _:
                tracking.state = IssueTrackingState.TRIAGED

        return tracking

    async def _escalate(self, issue: Issue, reason: str) -> None:
        """Escalate an issue to the repo owner."""
        await self._github.add_labels(
            self._owner, self._repo, issue.number, ["maintainer:escalated"]
        )
        await self._github.add_issue_comment(
            self._owner,
            self._repo,
            issue.number,
            f"⚠️ **Project Maintainer Escalation**\n\n"
            f"**Reason:** {reason}\n\n"
            f"This issue needs human attention.",
        )
