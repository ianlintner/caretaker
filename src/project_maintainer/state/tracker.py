"""Issue-backed state tracker for orchestrator persistence."""

from __future__ import annotations

import json
import logging
from typing import Any

from project_maintainer.github_client.api import GitHubClient

from .models import OrchestratorState, RunSummary

logger = logging.getLogger(__name__)

TRACKING_ISSUE_TITLE = "[Maintainer] Orchestrator State"
TRACKING_LABEL = "maintainer:internal"
STATE_MARKER_OPEN = "<!-- maintainer-state:"
STATE_MARKER_CLOSE = ":maintainer-state -->"


class StateTracker:
    """Persists orchestrator state as a hidden JSON block in a tracking issue."""

    def __init__(self, github: GitHubClient, owner: str, repo: str) -> None:
        self._github = github
        self._owner = owner
        self._repo = repo
        self._tracking_issue_number: int | None = None
        self._state: OrchestratorState = OrchestratorState()

    @property
    def state(self) -> OrchestratorState:
        return self._state

    async def load(self) -> OrchestratorState:
        """Load state from the tracking issue."""
        issue_number = await self._find_tracking_issue()
        if issue_number is None:
            logger.info("No tracking issue found — starting fresh")
            self._state = OrchestratorState()
            return self._state

        self._tracking_issue_number = issue_number
        comments = await self._github.get_pr_comments(
            self._owner, self._repo, issue_number
        )

        # Find the latest state comment (search from newest)
        for comment in reversed(comments):
            state_json = self._extract_state(comment.body)
            if state_json is not None:
                try:
                    self._state = OrchestratorState.model_validate_json(state_json)
                    logger.info("Loaded state from comment %d", comment.id)
                    return self._state
                except Exception:
                    logger.warning("Failed to parse state from comment %d", comment.id)

        logger.info("No valid state found in tracking issue — starting fresh")
        self._state = OrchestratorState()
        return self._state

    async def save(self, summary: RunSummary | None = None) -> None:
        """Save current state to the tracking issue."""
        if summary:
            self._state.last_run = summary
            self._state.run_history.append(summary)
            # Keep last 20 runs
            if len(self._state.run_history) > 20:
                self._state.run_history = self._state.run_history[-20:]

        if self._tracking_issue_number is None:
            await self._create_tracking_issue()

        assert self._tracking_issue_number is not None

        state_json = self._state.model_dump_json(indent=2)
        body = self._build_state_comment(state_json, summary)
        await self._github.add_issue_comment(
            self._owner, self._repo, self._tracking_issue_number, body
        )
        logger.info("State saved to tracking issue #%d", self._tracking_issue_number)

    async def post_run_summary(self, summary: RunSummary) -> None:
        """Post a human-readable run summary to the tracking issue."""
        if self._tracking_issue_number is None:
            await self._create_tracking_issue()
        assert self._tracking_issue_number is not None

        body = self._format_summary(summary)
        await self._github.add_issue_comment(
            self._owner, self._repo, self._tracking_issue_number, body
        )

    async def _find_tracking_issue(self) -> int | None:
        issues = await self._github.list_issues(
            self._owner, self._repo, labels=TRACKING_LABEL
        )
        for issue in issues:
            if issue.title == TRACKING_ISSUE_TITLE:
                return issue.number
        return None

    async def _create_tracking_issue(self) -> None:
        issue = await self._github.create_issue(
            self._owner,
            self._repo,
            title=TRACKING_ISSUE_TITLE,
            body=(
                "This issue is used by the project-maintainer orchestrator "
                "to track state and post run summaries.\n\n"
                "**Do not close or modify this issue.**\n\n"
                "Label: `maintainer:internal`"
            ),
            labels=[TRACKING_LABEL],
        )
        self._tracking_issue_number = issue.number
        logger.info("Created tracking issue #%d", issue.number)

    @staticmethod
    def _extract_state(body: str) -> str | None:
        start = body.find(STATE_MARKER_OPEN)
        if start == -1:
            return None
        start += len(STATE_MARKER_OPEN)
        end = body.find(STATE_MARKER_CLOSE, start)
        if end == -1:
            return None
        return body[start:end].strip()

    @staticmethod
    def _build_state_comment(state_json: str, summary: RunSummary | None = None) -> str:
        lines = ["## Orchestrator State Update\n"]
        if summary:
            lines.append(f"Run completed at {summary.run_at.isoformat()} (mode: {summary.mode})")
            lines.append(f"- PRs monitored: {summary.prs_monitored}")
            lines.append(f"- PRs merged: {summary.prs_merged}")
            lines.append(f"- Issues triaged: {summary.issues_triaged}")
            if summary.errors:
                lines.append(f"- Errors: {len(summary.errors)}")
            lines.append("")
        lines.append(f"{STATE_MARKER_OPEN}\n{state_json}\n{STATE_MARKER_CLOSE}")
        return "\n".join(lines)

    @staticmethod
    def _format_summary(summary: RunSummary) -> str:
        from datetime import datetime

        lines = [
            f"## Maintainer Run — {summary.run_at.strftime('%B %d, %Y')}",
            "",
            "### PR Agent",
            f"- {summary.prs_monitored} PRs monitored",
            f"- {summary.prs_merged} merged",
            f"- {summary.prs_escalated} escalated",
            "",
            "### Issue Agent",
            f"- {summary.issues_triaged} issues triaged",
            f"- {summary.issues_assigned} assigned to Copilot",
            f"- {summary.issues_closed} closed",
            f"- {summary.issues_escalated} escalated",
            "",
            "### Reconciliation",
            f"- {summary.orphaned_prs} orphaned PRs detected",
            f"- {summary.stale_assignments_escalated} stale assignments escalated",
            "",
        ]
        if summary.upgrade_available:
            lines.extend([
                "### Upgrade Agent",
                f"- Upgrade available: {summary.upgrade_version}",
                "",
            ])
        if summary.errors:
            lines.extend([
                "### Errors",
                *[f"- {e}" for e in summary.errors],
                "",
            ])
        return "\n".join(lines)
