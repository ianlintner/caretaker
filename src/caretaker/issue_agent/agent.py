"""Issue Agent — triages, classifies, and dispatches issues."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, cast

from caretaker.issue_agent.classifier import IssueClassification, classify_issue
from caretaker.issue_agent.dispatcher import IssueDispatcher
from caretaker.state.models import IssueTrackingState, TrackedIssue
from caretaker.tools.debug_dump import render_debug_dump
from caretaker.tools.github import GitHubIssueTools, GitHubPullRequestTools

if TYPE_CHECKING:
    from caretaker.config import IssueAgentConfig
    from caretaker.foundry.dispatcher import ExecutorDispatcher
    from caretaker.github_client.api import GitHubClient
    from caretaker.github_client.models import Issue
    from caretaker.llm.router import LLMRouter

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
        dispatcher: ExecutorDispatcher | None = None,
    ) -> None:
        self._github = github
        self._owner = owner
        self._repo = repo
        self._config = config
        self._llm = llm_router
        self._issues = GitHubIssueTools(github, owner, repo)
        self._pull_requests = GitHubPullRequestTools(github, owner, repo)
        self._dispatcher = IssueDispatcher(github, owner, repo, dispatcher=dispatcher)

    async def run(
        self, tracked_issues: dict[int, TrackedIssue]
    ) -> tuple[IssueAgentReport, dict[int, TrackedIssue]]:
        """Run the issue agent — triage all open issues."""
        report = IssueAgentReport()

        issues = await self._issues.list(state="all")
        pull_requests = await self._pull_requests.list(state="all")

        for issue in issues:
            try:
                tracking = tracked_issues.get(issue.number, TrackedIssue(number=issue.number))

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

                # Skip issues that are already actioned or in-flight
                if tracking.state in (
                    IssueTrackingState.ASSIGNED,
                    IssueTrackingState.IN_PROGRESS,
                    IssueTrackingState.PR_OPENED,
                    IssueTrackingState.ESCALATED,
                    IssueTrackingState.STALE,
                    IssueTrackingState.COMPLETED,
                    IssueTrackingState.CLOSED,
                ):
                    tracking.last_checked = datetime.utcnow()
                    tracked_issues[issue.number] = tracking
                    continue

                classification = classify_issue(issue, self._config)
                tracking.classification = classification.value
                report.triaged += 1

                tracking = await self._process_issue(issue, classification, tracking, report)
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
                await self._escalate(
                    issue,
                    "Large feature — needs human decomposition",
                    debug_data={"classification": classification.value},
                )
                tracking.state = IssueTrackingState.ESCALATED
                report.escalated.append(issue.number)

            case IssueClassification.QUESTION:
                if self._config.auto_close_questions:
                    await self._issues.comment(
                        issue.number,
                        "This issue has been classified as a question. "
                        "Please check the project documentation and README for guidance. "
                        "If this needs further attention, please reopen.",
                    )
                    await self._issues.update(issue.number, state="closed")
                    tracking.state = IssueTrackingState.CLOSED
                    report.closed.append(issue.number)

            case IssueClassification.DUPLICATE:
                await self._issues.comment(
                    issue.number,
                    "This issue appears to be a duplicate. "
                    "If needed, please reference the original tracking issue.",
                )
                await self._issues.update(issue.number, state="closed")
                tracking.state = IssueTrackingState.CLOSED
                report.closed.append(issue.number)

            case IssueClassification.STALE:
                await self._issues.comment(
                    issue.number,
                    "Closing as stale due to inactivity. Please reopen if this is still relevant.",
                )
                await self._issues.update(issue.number, state="closed")
                tracking.state = IssueTrackingState.STALE
                report.closed.append(issue.number)

            case IssueClassification.INFRA_OR_CONFIG:
                await self._escalate(
                    issue,
                    "Infrastructure/config issue — requires human access",
                    debug_data={"classification": classification.value},
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
        pull_requests: list[Any],
        tracking: TrackedIssue,
    ) -> TrackedIssue:
        """Update issue tracking state based on assignees and linked PRs."""
        is_copilot = issue.is_copilot_assigned
        if is_copilot and tracking.state in (IssueTrackingState.NEW, IssueTrackingState.TRIAGED):
            tracking.state = IssueTrackingState.IN_PROGRESS

        linked_pr = self._find_linked_pr_number(issue.number, pull_requests)
        if linked_pr is not None:
            tracking.assigned_pr = linked_pr
            tracking.state = IssueTrackingState.PR_OPENED

        # If we previously dispatched this issue but Copilot is no longer assigned
        # and there is no linked PR, downgrade so the issue can be re-dispatched.
        if (
            tracking.state in (IssueTrackingState.ASSIGNED, IssueTrackingState.IN_PROGRESS)
            and not is_copilot
            and linked_pr is None
        ):
            logger.info(
                "Issue #%d: no Copilot assignee and no linked PR — resetting to TRIAGED",
                issue.number,
            )
            tracking.state = IssueTrackingState.TRIAGED

        return tracking

    @staticmethod
    def _find_linked_pr_number(issue_number: int, pull_requests: list[Any]) -> int | None:
        """Find a PR that links to an issue via closing keywords or plain references."""
        issue_ref = f"#{issue_number}"
        link_pattern = re.compile(
            rf"\b(fix|fixes|fixed|close|closes|closed|resolve|resolves|resolved)\s+{re.escape(issue_ref)}\b",
            re.IGNORECASE,
        )

        for pr in pull_requests:
            text = f"{pr.title}\n{pr.body or ''}"
            if issue_ref in text and (link_pattern.search(text) or issue_ref in (pr.body or "")):
                return cast("int", cast("Any", pr).number)

        return None

    async def _escalate(
        self,
        issue: Issue,
        reason: str,
        *,
        debug_data: dict[str, Any] | None = None,
    ) -> None:
        """Escalate an issue to the repo owner."""
        await self._issues.add_labels(issue.number, ["maintainer:escalated"])
        payload: dict[str, Any] = {
            "type": "issue_escalation",
            "owner": self._owner,
            "repo": self._repo,
            "issue": {
                "number": issue.number,
                "title": issue.title,
                "state": issue.state,
                "labels": [label.name for label in issue.labels],
                "assignees": [assignee.login for assignee in issue.assignees],
                "updated_at": issue.updated_at,
                "html_url": issue.html_url,
            },
            "reason": reason,
        }
        if debug_data:
            payload["debug"] = debug_data

        body = (
            f"⚠️ **Caretaker Escalation**\n\n"
            f"**Reason:** {reason}\n\n"
            f"This issue needs human attention."
        )
        body += render_debug_dump(payload, title="Escalation debug dump")
        await self._issues.comment(
            issue.number,
            body,
        )
