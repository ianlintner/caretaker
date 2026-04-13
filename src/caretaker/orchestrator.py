"""Orchestrator — wires all agents together and runs the main loop."""

from __future__ import annotations

import logging
import os
from datetime import datetime

from caretaker import __version__
from caretaker.config import MaintainerConfig
from caretaker.github_client.api import GitHubClient
from caretaker.devops_agent.agent import DevOpsAgent
from caretaker.issue_agent.agent import IssueAgent
from caretaker.llm.router import LLMRouter
from caretaker.pr_agent.agent import PRAgent
from caretaker.self_heal_agent.agent import SelfHealAgent
from caretaker.state.models import (
    IssueTrackingState,
    OrchestratorState,
    PRTrackingState,
    RunSummary,
)
from caretaker.state.tracker import StateTracker
from caretaker.upgrade_agent.agent import UpgradeAgent

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

    @classmethod
    def from_config_path(cls, path: str) -> Orchestrator:
        """Create an orchestrator from a YAML config file path."""
        config = MaintainerConfig.from_yaml(path)

        token = os.environ.get("GITHUB_TOKEN", "")
        if not token:
            raise RuntimeError("GITHUB_TOKEN environment variable is required")

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

        github = GitHubClient(token=token)
        return cls(config=config, github=github, owner=owner, repo=repo_name)

    async def run(
        self,
        mode: str = "full",
        event_type: str | None = None,
        event_payload: dict | None = None,
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
        state = await self._state_tracker.load()
        summary = RunSummary(mode=mode, run_at=datetime.utcnow())
        has_errors = False

        try:
            # Event-driven mode — route to specific agent
            if mode == "event" and event_type:
                await self._handle_event(event_type, event_payload or {}, state, summary)
            else:
                # Scheduled / full mode
                if mode in ("full", "pr-only"):
                    await self._run_pr_agent(state, summary)

                if mode in ("full", "issue-only"):
                    await self._run_issue_agent(state, summary)

                if mode in ("full", "upgrade"):
                    await self._run_upgrade_agent(state, summary)

                if mode in ("full", "devops"):
                    await self._run_devops_agent(state, summary)

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

            if tracked_issue.state in (
                IssueTrackingState.ASSIGNED,
                IssueTrackingState.IN_PROGRESS,
            ):
                if tracked_issue.last_checked is not None:
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
                    (tracked_pr.merged_at - tracked_pr.first_seen_at).total_seconds()
                    / 3600.0
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
        payload: dict,
        state: OrchestratorState,
        summary: RunSummary,
    ) -> None:
        """Handle a single GitHub event."""
        logger.info("Handling event: %s", event_type)

        if event_type in ("pull_request", "pull_request_review", "check_run", "check_suite"):
            await self._run_pr_agent(state, summary)
        elif event_type in ("issues", "issue_comment"):
            await self._run_issue_agent(state, summary)
        elif event_type == "workflow_run":
            # A workflow completed — run devops (CI failures) and self-heal
            await self._run_devops_agent(state, summary, event_payload=payload)
            await self._run_self_heal_agent(state, summary, event_payload=payload)
        else:
            logger.info("Event type %s — running full cycle", event_type)
            await self._run_pr_agent(state, summary)
            await self._run_issue_agent(state, summary)

    async def _run_pr_agent(
        self, state: OrchestratorState, summary: RunSummary
    ) -> None:
        """Run the PR agent."""
        if not self._config.pr_agent.enabled:
            logger.info("PR agent is disabled")
            return

        if self._config.orchestrator.dry_run:
            logger.info("[DRY RUN] PR agent would run")
            return

        pr_agent = PRAgent(
            github=self._github,
            owner=self._owner,
            repo=self._repo,
            config=self._config.pr_agent,
            llm_router=self._llm,
        )
        report, tracked_prs = await pr_agent.run(state.tracked_prs)
        state.tracked_prs = tracked_prs

        summary.prs_monitored = report.monitored
        summary.prs_merged = len(report.merged)
        summary.prs_escalated = len(report.escalated)
        summary.errors.extend(report.errors)
        summary.prs_fix_requested = len(report.fix_requested)

    async def _run_issue_agent(
        self, state: OrchestratorState, summary: RunSummary
    ) -> None:
        """Run the issue agent."""
        if not self._config.issue_agent.enabled:
            logger.info("Issue agent is disabled")
            return

        if self._config.orchestrator.dry_run:
            logger.info("[DRY RUN] Issue agent would run")
            return

        issue_agent = IssueAgent(
            github=self._github,
            owner=self._owner,
            repo=self._repo,
            config=self._config.issue_agent,
            llm_router=self._llm,
        )
        report, tracked_issues = await issue_agent.run(state.tracked_issues)
        state.tracked_issues = tracked_issues

        summary.issues_triaged = report.triaged
        summary.issues_assigned = len(report.assigned)
        summary.issues_closed = len(report.closed)
        summary.issues_escalated = len(report.escalated)
        summary.errors.extend(report.errors)

    async def _run_upgrade_agent(
        self, state: OrchestratorState, summary: RunSummary
    ) -> None:
        """Run the upgrade agent."""
        if not self._config.upgrade_agent.enabled:
            logger.info("Upgrade agent is disabled")
            return

        if self._config.orchestrator.dry_run:
            logger.info("[DRY RUN] Upgrade agent would run")
            return

        upgrade_agent = UpgradeAgent(
            github=self._github,
            owner=self._owner,
            repo=self._repo,
            config=self._config.upgrade_agent,
            current_version=__version__,
        )
        report = await upgrade_agent.run()

        summary.upgrade_available = report.upgrade_needed
        summary.upgrade_version = report.latest_version or ""
        summary.errors.extend(report.errors)

    async def _run_devops_agent(
        self,
        state: OrchestratorState,
        summary: RunSummary,
        event_payload: dict | None = None,
    ) -> None:
        """Run the DevOps agent to triage CI build failures on the default branch."""
        cfg = self._config.devops_agent
        if not cfg.enabled:
            logger.info("DevOps agent is disabled")
            return

        if self._config.orchestrator.dry_run:
            logger.info("[DRY RUN] DevOps agent would run")
            return

        agent = DevOpsAgent(
            github=self._github,
            owner=self._owner,
            repo=self._repo,
            default_branch=cfg.target_branch,
            max_issues_per_run=cfg.max_issues_per_run,
        )
        report = await agent.run(event_payload=event_payload)

        summary.build_failures_detected = report.failures_detected
        summary.build_fix_issues_created = len(report.issues_created)
        summary.errors.extend(report.errors)

    async def _run_self_heal_agent(
        self,
        state: OrchestratorState,
        summary: RunSummary,
        event_payload: dict | None = None,
    ) -> None:
        """Run the self-heal agent to diagnose and fix caretaker's own failures."""
        cfg = self._config.self_heal_agent
        if not cfg.enabled:
            logger.info("Self-heal agent is disabled")
            return

        if self._config.orchestrator.dry_run:
            logger.info("[DRY RUN] Self-heal agent would run")
            return

        report_upstream = cfg.report_upstream and not cfg.is_upstream_repo
        agent = SelfHealAgent(
            github=self._github,
            owner=self._owner,
            repo=self._repo,
            report_upstream=report_upstream,
        )
        report = await agent.run(event_payload=event_payload)

        summary.self_heal_failures_analyzed = report.failures_analyzed
        summary.self_heal_local_issues = len(report.local_issues_created)
        summary.self_heal_upstream_bugs = len(report.upstream_issues_opened)
        summary.self_heal_upstream_features = len(report.upstream_features_requested)
        summary.errors.extend(report.errors)
