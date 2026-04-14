"""Orchestrator — wires all agents together and runs the main loop."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import TYPE_CHECKING, Any

from caretaker import __version__
from caretaker.agent_protocol import AgentContext
from caretaker.agents import EVENT_AGENT_MAP, build_registry
from caretaker.config import MaintainerConfig
from caretaker.github_client.api import GitHubClient
from caretaker.llm.router import LLMRouter
from caretaker.state.models import (
    IssueTrackingState,
    OrchestratorState,
    PRTrackingState,
    RunSummary,
)
from caretaker.state.tracker import StateTracker

if TYPE_CHECKING:
    from caretaker.registry import AgentRegistry

logger = logging.getLogger(__name__)


class Orchestrator:
    """Central orchestrator that coordinates all agents."""

    def __init__(
        self,
        config: MaintainerConfig,
        github: GitHubClient,
        owner: str,
        repo: str,
    ) -> None:
        self._config = config
        self._github = github
        self._owner = owner
        self._repo = repo
        self._llm = LLMRouter(config.llm)
        self._state_tracker = StateTracker(github, owner, repo)

        ctx = AgentContext(
            github=github,
            owner=owner,
            repo=repo,
            config=config,
            llm_router=self._llm,
            dry_run=config.orchestrator.dry_run,
        )
        self._registry: AgentRegistry = build_registry(ctx)

    @classmethod
    def from_config_path(cls, path: str) -> Orchestrator:
        """Create an orchestrator from a YAML config file path."""
        config = MaintainerConfig.from_yaml(path)

        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("COPILOT_PAT", "")
        if not token:
            raise RuntimeError("GITHUB_TOKEN or COPILOT_PAT environment variable is required")

        owner = os.environ.get("GITHUB_REPOSITORY_OWNER", "")
        repo_name = os.environ.get("GITHUB_REPOSITORY_NAME", "")

        # Fall back to GITHUB_REPOSITORY (owner/repo format)
        if not owner or not repo_name:
            full = os.environ.get("GITHUB_REPOSITORY", "")
            if "/" in full:
                owner, repo_name = full.split("/", 1)
            else:
                raise RuntimeError(
                    "GITHUB_REPOSITORY or GITHUB_REPOSITORY_OWNER + "
                    "GITHUB_REPOSITORY_NAME environment variables are required"
                )

        github = GitHubClient(token=token, copilot_token=os.environ.get("COPILOT_PAT"))
        return cls(config=config, github=github, owner=owner, repo=repo_name)

    async def run(
        self,
        mode: str = "full",
        event_type: str | None = None,
        event_payload: dict[str, Any] | None = None,
        report_path: str | None = None,
    ) -> int:
        """Run the orchestrator. Returns 0 on success, 1 on errors."""
        logger.info(
            "Orchestrator starting — mode=%s, version=%s, repo=%s/%s",
            mode,
            __version__,
            self._owner,
            self._repo,
        )

        # Load persisted state
        state = OrchestratorState()
        summary = RunSummary(mode=mode, run_at=datetime.utcnow())
        has_errors = False

        try:
            state = await self._state_tracker.load()
            # Event-driven mode — route to specific agent
            if mode == "event" and event_type:
                await self._handle_event(event_type, event_payload or {}, state, summary)
            else:
                # Dry-run evaluates full mode with read-only behavior controlled by context.
                dispatch_mode = "full" if mode == "dry-run" else mode
                # Scheduled mode — run every agent registered for this mode
                await self._registry.run_all(
                    state,
                    summary,
                    mode=dispatch_mode,
                    event_payload=event_payload or {},
                )

            # Cross-agent state reconciliation
            self._reconcile_state(state, summary)

        except Exception as e:
            logger.error("Orchestrator error: %s", e, exc_info=True)
            summary.errors.append(str(e))
            has_errors = True

        # Persist state (save also appends summary to history)
        await self._state_tracker.save(summary)

        # Post summary if configured
        if self._config.orchestrator.summary_issue and mode != "dry-run":
            try:
                await self._state_tracker.post_run_summary(summary)
            except Exception as e:
                logger.warning("Failed to post summary: %s", e)

        if summary.errors:
            has_errors = True
            logger.warning("Run completed with %d errors", len(summary.errors))
        else:
            logger.info("Run completed successfully")

        # Write JSON run report if a path was provided
        if report_path:
            try:
                report_data = summary.model_dump(mode="json")
                with open(report_path, "w", encoding="utf-8") as fh:
                    json.dump(report_data, fh, indent=2, default=str)
                logger.info("Run report written to %s", report_path)
            except Exception as e:
                logger.warning("Failed to write run report: %s", e)

        return 1 if has_errors else 0

    def _reconcile_state(self, state: OrchestratorState, summary: RunSummary) -> None:
        """Reconcile cross-agent tracked PR/issue state and derive reconciliation metrics."""
        now = datetime.utcnow()

        issue_to_pr: dict[int, int] = {
            issue_number: tracked_issue.assigned_pr
            for issue_number, tracked_issue in state.tracked_issues.items()
            if tracked_issue.assigned_pr is not None
        }

        linked_pr_numbers = set(issue_to_pr.values())
        orphaned_prs = 0
        for pr_number, tracked_pr in state.tracked_prs.items():
            if tracked_pr.state in (PRTrackingState.MERGED, PRTrackingState.CLOSED):
                continue
            if pr_number not in linked_pr_numbers:
                orphaned_prs += 1
        summary.orphaned_prs = orphaned_prs

        stale_escalated = 0
        for tracked_issue in state.tracked_issues.values():
            if tracked_issue.assigned_pr is not None:
                pr = state.tracked_prs.get(tracked_issue.assigned_pr)
                if pr is not None:
                    if pr.state == PRTrackingState.MERGED:
                        tracked_issue.state = IssueTrackingState.COMPLETED
                    elif pr.state == PRTrackingState.CLOSED:
                        tracked_issue.state = IssueTrackingState.CLOSED
                    elif pr.state == PRTrackingState.ESCALATED:
                        tracked_issue.state = IssueTrackingState.ESCALATED

            if (
                tracked_issue.state
                in (
                    IssueTrackingState.ASSIGNED,
                    IssueTrackingState.IN_PROGRESS,
                )
                and tracked_issue.last_checked is not None
            ):
                age_days = (now - tracked_issue.last_checked).days
                if age_days >= self._config.escalation.stale_days:
                    tracked_issue.state = IssueTrackingState.ESCALATED
                    tracked_issue.escalated = True
                    stale_escalated += 1

        summary.stale_assignments_escalated = stale_escalated

        total_work_items = summary.prs_monitored + summary.issues_triaged
        total_escalated = summary.prs_escalated + summary.issues_escalated
        summary.escalation_rate = (
            total_escalated / total_work_items if total_work_items > 0 else 0.0
        )

        merged_durations_hours: list[float] = []
        for tracked_pr in state.tracked_prs.values():
            if tracked_pr.merged_at and tracked_pr.first_seen_at:
                merged_durations_hours.append(
                    (tracked_pr.merged_at - tracked_pr.first_seen_at).total_seconds() / 3600.0
                )
        if merged_durations_hours:
            summary.avg_time_to_merge_hours = sum(merged_durations_hours) / len(
                merged_durations_hours
            )

        if summary.prs_monitored > 0:
            summary.copilot_success_rate = summary.prs_merged / summary.prs_monitored

    async def _handle_event(
        self,
        event_type: str,
        payload: dict[str, Any],
        state: OrchestratorState,
        summary: RunSummary,
    ) -> None:
        """Handle a single GitHub event by dispatching to specific agents."""
        logger.info("Handling event: %s", event_type)

        agent_names = EVENT_AGENT_MAP.get(event_type)
        if agent_names is None:
            # Unknown event — fall back to PR + Issue
            logger.info("Event type %s — running full cycle", event_type)
            for name in ("pr", "issue"):
                agent = self._registry.get(name)
                if agent:
                    await self._registry.run_one(agent, state, summary)
            return

        for name in agent_names:
            agent = self._registry.get(name)
            if not agent:
                continue

            if name == "pr" and event_type == "workflow_run":
                head_branch: str | None = payload.get("workflow_run", {}).get("head_branch")
                await self._registry.run_one(
                    agent,
                    state,
                    summary,
                    event_payload={"_head_branch": head_branch},
                )
            elif name in ("devops", "self-heal"):
                await self._registry.run_one(
                    agent,
                    state,
                    summary,
                    event_payload=payload,
                )
            else:
                await self._registry.run_one(agent, state, summary)
