"""Issue dispatcher — creates structured Copilot assignments."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from caretaker.causal import extract_causal, make_causal_marker
from caretaker.foundry.dispatcher import (
    LABEL_AGENT_COPILOT,
    LABEL_AGENT_CUSTOM,
    LABEL_AGENT_QUARANTINE,
    routing_override,
)
from caretaker.issue_agent.classifier import IssueClassification
from caretaker.tools.github import GitHubIssueTools

if TYPE_CHECKING:
    from caretaker.foundry.dispatcher import ExecutorDispatcher
    from caretaker.github_client.api import GitHubClient
    from caretaker.github_client.models import Issue

logger = logging.getLogger(__name__)


def build_assignment_body(issue: Issue, classification: IssueClassification) -> str:
    """Build a structured assignment body for Copilot."""
    parent_causal = extract_causal(issue.body or "")
    parent_id = parent_causal["id"] if parent_causal else None
    lines = [
        f"## [Maintainer] Fix: {issue.title}",
        "",
        f"Fixes #{issue.number} (reported by @{issue.user.login})",
        "",
        "@copilot Please implement this fix. "
        "See `.github/agents/maintainer-issue.md` for your workflow.",
        "",
        make_causal_marker("issue-agent:dispatch", parent=parent_id),
        "<!-- caretaker:assignment -->",
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
        "<!-- /caretaker:assignment -->",
    ]
    return "\n".join(lines)


class IssueDispatcher:
    """Creates structured issue assignments for Copilot.

    Accepts an optional :class:`ExecutorDispatcher` for symmetry with the
    other bridges.  MVP: the dispatcher is stored but not yet consumed — small
    feature / simple-bug routing through Foundry is Phase 2 since it requires
    design decomposition the tool-loop doesn't yet do.
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
        # TODO(foundry-phase-2): route FEATURE_SMALL / BUG_SIMPLE tasks
        # through this dispatcher once we have an issue-to-PR decomposer.
        self._dispatcher = dispatcher

    @property
    def dispatcher(self) -> ExecutorDispatcher | None:
        return self._dispatcher

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

        # Label overrides honored at the issue level too. The custom-executor
        # route for issues is opt-in via the consumer-side
        # ``agent-custom.yml`` workflow that picks up the label; here we
        # skip the Copilot assignment and apply the routing label so the
        # GHA workflow (or an operator) can take it from there.
        override = routing_override(issue.labels)
        if override == LABEL_AGENT_QUARANTINE:
            logger.info(
                "Issue #%d not dispatched: agent:quarantine label present",
                issue.number,
            )
            return None
        if override == LABEL_AGENT_CUSTOM:
            parent_causal = extract_causal(issue.body or "")
            parent_id = parent_causal["id"] if parent_causal else None
            causal = make_causal_marker("issue-agent:dispatch", parent=parent_id)
            await self._issues.comment(
                issue.number,
                (
                    f"@{issue.user.login} this issue is labeled `{LABEL_AGENT_CUSTOM}` "
                    "and will be picked up by the caretaker custom coding agent "
                    "rather than Copilot. See `docs/custom-coding-agent-plan.md` for details.\n\n"
                    f"{causal}"
                ),
            )
            logger.info(
                "Issue #%d deferred to custom executor via agent:custom label", issue.number
            )
            return None

        body = build_assignment_body(issue, classification)
        copilot_assignment = self._issues.default_copilot_assignment()

        # agent:copilot is an explicit no-op override (just keeps the
        # default Copilot path) but we annotate the log so operators can
        # see the decision was label-driven, not config-driven.
        if override == LABEL_AGENT_COPILOT:
            logger.info("Issue #%d routed to Copilot by agent:copilot label", issue.number)

        # For simple bugs, just update the existing issue and assign to Copilot
        if classification == IssueClassification.BUG_SIMPLE:
            parent_causal = extract_causal(issue.body or "")
            parent_id = parent_causal["id"] if parent_causal else None
            causal = make_causal_marker("issue-agent:dispatch", parent=parent_id)
            comment_body = (
                f"@copilot This issue has been triaged as `{classification.value}`. "
                "Please fix it.\n\n"
                "See `.github/agents/maintainer-issue.md` for your workflow.\n\n"
                f"{causal}\n"
                "<!-- caretaker:assignment -->\n"
                f"TYPE: {classification.value}\n"
                "PRIORITY: medium\n"
                "<!-- /caretaker:assignment -->"
            )
            await self._issues.comment(issue.number, comment_body)
            await self._issues.update(
                issue.number,
                assignees=["copilot"],
                labels=[lbl.name for lbl in issue.labels] + ["maintainer:assigned"],
                copilot_assignment=copilot_assignment,
            )
            logger.info("Issue #%d assigned to Copilot as %s", issue.number, classification.value)
            return issue

        # For features, create a new structured issue
        if classification in (
            IssueClassification.FEATURE_SMALL,
            IssueClassification.BUG_COMPLEX,
        ):
            new_issue = await self._issues.create(
                title=f"[Maintainer] {issue.title}",
                body=body,
                labels=["maintainer:internal", "maintainer:assigned"],
                assignees=["copilot"],
                copilot_assignment=copilot_assignment,
            )
            # Link back to original
            await self._issues.comment(
                issue.number,
                f"This issue has been picked up by the caretaker. Tracking in #{new_issue.number}.",
            )
            logger.info(
                "Created assignment issue #%d for source issue #%d",
                new_issue.number,
                issue.number,
            )
            return new_issue

        return None
