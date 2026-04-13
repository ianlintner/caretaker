"""Issue Agent — triages, classifies, and dispatches issues."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime

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

        issues = await self._github.list_issues(self._owner, self._repo, state="all")
        pull_requests = await self._github.list_pull_requests(
            self._owner, self._repo, state="all"
        )

        for issue in issues:
            try:
                tracking = tracked_issues.get(
                    issue.number, TrackedIssue(number=issue.number)
                )

                # Reconcile closed issues and stop processing
                if issue.state != "open":
                    if tracking.state in (
                        IssueTrackingState.PR_OPENED,
                        IssueTrackingState.IN_PROGRESS,
                        IssueTrackingState.ASSIGNED,
                    ):
                        tracking.state = IssueTrackingState.COMPLETED
                    else:
                        tracking.state = IssueTrackingState.CLOSED
                    tracking.last_checked = datetime.utcnow()
                    tracked_issues[issue.number] = tracking
                    continue

                tracking = self._reconcile_issue_progress(issue, pull_requests, tracking)

                # Skip already-processed issues
                if tracking.state in (
                    IssueTrackingState.COMPLETED,
                    IssueTrackingState.CLOSED,
                ):
                    tracking.last_checked = datetime.utcnow()
                    tracked_issues[issue.number] = tracking
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
                    if tracking.state in (
                        IssueTrackingState.NEW,
                        IssueTrackingState.TRIAGED,
                    ):
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

            case IssueClassification.DUPLICATE:
                await self._github.add_issue_comment(
                    self._owner,
                    self._repo,
                    issue.number,
                    "This issue appears to be a duplicate. "
                    "If needed, please reference the original tracking issue.",
                )
                await self._github.update_issue(
                    self._owner, self._repo, issue.number, state="closed"
                )
                tracking.state = IssueTrackingState.CLOSED
                report.closed.append(issue.number)

            case IssueClassification.STALE:
                await self._github.add_issue_comment(
                    self._owner,
                    self._repo,
                    issue.number,
                    "Closing as stale due to inactivity. "
                    "Please reopen if this is still relevant.",
                )
                await self._github.update_issue(
                    self._owner, self._repo, issue.number, state="closed"
                )
                tracking.state = IssueTrackingState.STALE
                report.closed.append(issue.number)

            case IssueClassification.INFRA_OR_CONFIG:
                await self._escalate(
                    issue, "Infrastructure/config issue — requires human access"
                )
                tracking.state = IssueTrackingState.ESCALATED
                report.escalated.append(issue.number)

            case _:
                if tracking.state in (
                    IssueTrackingState.NEW,
                    IssueTrackingState.TRIAGED,
                ):
                    tracking.state = IssueTrackingState.TRIAGED

        return tracking

    def _reconcile_issue_progress(
        self,
        issue: Issue,
        pull_requests: list,
        tracking: TrackedIssue,
    ) -> TrackedIssue:
        """Update issue tracking state based on assignees and linked PRs."""
        if any(a.login in ("copilot", "copilot[bot]", "github-copilot[bot]") for a in issue.assignees):
            if tracking.state in (IssueTrackingState.NEW, IssueTrackingState.TRIAGED):
                tracking.state = IssueTrackingState.IN_PROGRESS

        linked_pr = self._find_linked_pr_number(issue.number, pull_requests)
        if linked_pr is not None:
            tracking.assigned_pr = linked_pr
            tracking.state = IssueTrackingState.PR_OPENED

        return tracking

    @staticmethod
    def _find_linked_pr_number(issue_number: int, pull_requests: list) -> int | None:
        """Find a PR that links to an issue via closing keywords or plain references."""
        issue_ref = f"#{issue_number}"
        link_pattern = re.compile(
            rf"\b(fix|fixes|fixed|close|closes|closed|resolve|resolves|resolved)\s+{re.escape(issue_ref)}\b",
            re.IGNORECASE,
        )

        for pr in pull_requests:
            text = f"{pr.title}\n{pr.body or ''}"
            if issue_ref in text and (
                link_pattern.search(text) or issue_ref in (pr.body or "")
            ):
                return pr.number

        return None

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
