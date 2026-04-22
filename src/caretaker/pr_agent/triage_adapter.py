"""BaseAgent adapter for the unified PR/issue triage + cascade pass.

Runs after the PR and issue agents in the main loop. Uses ``TriageConfig`` to
gate individual behaviors; the adapter itself is always registered so a config
toggle alone enables or disables triage without code changes.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from caretaker.agent_protocol import AgentResult, BaseAgent
from caretaker.issue_agent.issue_triage import run_issue_triage
from caretaker.pr_agent.cascade import (
    apply_cascade,
    on_pr_closed_unmerged,
    on_pr_merged,
)
from caretaker.pr_agent.pr_triage import run_pr_triage

if TYPE_CHECKING:
    from caretaker.github_client.models import PullRequest
    from caretaker.state.models import OrchestratorState, RunSummary

logger = logging.getLogger(__name__)


class TriageAgentAdapter(BaseAgent):
    """Triage + cascade cleanup across PRs and issues."""

    @property
    def name(self) -> str:
        return "triage"

    def enabled(self) -> bool:
        return self._ctx.config.triage.enabled

    async def execute(
        self,
        state: OrchestratorState,
        event_payload: dict[str, Any] | None = None,
    ) -> AgentResult:
        cfg = self._ctx.config.triage
        gh = self._ctx.github
        owner = self._ctx.owner
        repo = self._ctx.repo

        open_prs: list[PullRequest] = await gh.list_pull_requests(owner, repo, state="open")
        all_prs: list[PullRequest] = await gh.list_pull_requests(owner, repo, state="all")
        open_issues = await gh.list_issues(owner, repo, state="open")

        pr_report = await run_pr_triage(gh, owner, repo, open_prs, cfg)

        merged_prs = [pr for pr in all_prs if pr.merged]
        issue_report = await run_issue_triage(
            gh, owner, repo, open_issues, merged_prs, state.tracked_issues, cfg
        )

        cascade_actions = []
        if cfg.cascade:
            for pr in merged_prs:
                cascade_actions.extend(on_pr_merged(pr, state.tracked_issues))
            closed_unmerged = [pr for pr in all_prs if pr.state.value == "closed" and not pr.merged]
            for pr in closed_unmerged:
                cascade_actions.extend(on_pr_closed_unmerged(pr, state.tracked_issues))

        cascade_report = await apply_cascade(
            gh,
            owner,
            repo,
            cascade_actions,
            state.tracked_issues,
            dry_run=cfg.dry_run,
        )

        logger.info(
            "Triage agent%s: pr(empty=%d duplicate=%d conflicted=%d) "
            "issue(empty=%d duplicate=%d stale=%d resolved=%d) "
            "cascade(planned=%d applied=%d skipped=%d)",
            " [DRY RUN]" if cfg.dry_run else "",
            len(pr_report.closed_empty),
            len(pr_report.closed_duplicate),
            len(pr_report.closed_conflicted),
            len(issue_report.closed_empty),
            len(issue_report.closed_duplicate),
            len(issue_report.closed_stale),
            len(issue_report.closed_resolved),
            len(cascade_actions),
            len(cascade_report.applied),
            len(cascade_report.skipped),
        )

        errors = [*pr_report.errors, *issue_report.errors, *cascade_report.errors]
        processed = (
            len(pr_report.closed_empty)
            + len(pr_report.closed_duplicate)
            + len(pr_report.closed_conflicted)
            + len(issue_report.closed_empty)
            + len(issue_report.closed_duplicate)
            + len(issue_report.closed_stale)
            + len(issue_report.closed_resolved)
            + len(cascade_report.applied)
        )
        return AgentResult(
            processed=processed,
            errors=errors,
            extra={
                "pr_closed_empty": pr_report.closed_empty,
                "pr_closed_duplicate": pr_report.closed_duplicate,
                "pr_closed_conflicted": pr_report.closed_conflicted,
                "issue_closed_empty": issue_report.closed_empty,
                "issue_closed_duplicate": issue_report.closed_duplicate,
                "issue_closed_stale": issue_report.closed_stale,
                "issue_closed_resolved": issue_report.closed_resolved,
                "cascade_applied": [a.kind.value for a in cascade_report.applied],
            },
        )

    def apply_summary(self, result: AgentResult, summary: RunSummary) -> None:
        # Fold triage into existing closed/orphaned counters; no new fields.
        summary.issues_closed += len(result.extra.get("issue_closed_empty", []))
        summary.issues_closed += len(result.extra.get("issue_closed_duplicate", []))
        summary.issues_closed += len(result.extra.get("issue_closed_stale", []))
        summary.issues_closed += len(result.extra.get("issue_closed_resolved", []))
