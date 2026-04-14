"""Orchestrator — wires all agents together and runs the main loop."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Any

from caretaker import __version__
from caretaker.config import MaintainerConfig
from caretaker.dependency_agent.agent import DependencyAgent
from caretaker.devops_agent.agent import DevOpsAgent
from caretaker.docs_agent.agent import DocsAgent
from caretaker.escalation_agent.agent import EscalationAgent
from caretaker.github_client.api import GitHubClient
from caretaker.issue_agent.agent import IssueAgent
from caretaker.llm.router import LLMRouter
from caretaker.pr_agent.agent import PRAgent
from caretaker.security_agent.agent import SecurityAgent
from caretaker.self_heal_agent.agent import SelfHealAgent
from caretaker.stale_agent.agent import StaleAgent
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
        state = await self._state_tracker.load()
        summary = RunSummary(mode=mode, run_at=datetime.utcnow())
        has_errors = False

        try:
            # Event-driven mode — route to specific agent
            if mode == "self-heal":
                await self._run_self_heal_agent(state, summary, event_payload=event_payload)
            elif mode == "event" and event_type:
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

                if mode in ("full", "security"):
                    await self._run_security_agent(state, summary)

                if mode in ("full", "deps"):
                    await self._run_dependency_agent(state, summary)

                if mode in ("full", "docs"):
                    await self._run_docs_agent(state, summary)

                if mode in ("full", "stale"):
                    await self._run_stale_agent(state, summary)

                if mode in ("full", "escalation"):
                    await self._run_escalation_agent(state, summary)

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
        """Handle a single GitHub event."""
        logger.info("Handling event: %s", event_type)

        if event_type in ("pull_request", "pull_request_review", "check_run", "check_suite"):
            await self._run_pr_agent(state, summary)
        elif event_type in ("issues", "issue_comment"):
            await self._run_issue_agent(state, summary)
        elif event_type == "workflow_run":
            # A workflow completed — run devops (CI failures on default branch) and self-heal
            await self._run_devops_agent(state, summary, event_payload=payload)
            await self._run_self_heal_agent(state, summary, event_payload=payload)
            # Also evaluate the PR on the affected branch so CI failures trigger
            # @copilot fix requests promptly, without scanning every open PR.
            head_branch: str | None = (
                payload.get("workflow_run", {}).get("head_branch") if payload else None
            )
            await self._run_pr_agent(state, summary, head_branch=head_branch)
        elif event_type == "dependabot_alert":
            # A new Dependabot alert was raised
            await self._run_security_agent(state, summary)
        else:
            logger.info("Event type %s — running full cycle", event_type)
            await self._run_pr_agent(state, summary)
            await self._run_issue_agent(state, summary)

    async def _run_pr_agent(
        self,
        state: OrchestratorState,
        summary: RunSummary,
        head_branch: str | None = None,
    ) -> None:
        """Run the PR agent.

        Args:
            state: Current orchestrator state.
            summary: Run summary to update.
            head_branch: Optional branch name to restrict evaluation to a single
                PR branch (used for event-driven ``workflow_run`` invocations to
                avoid scanning every open PR on every CI completion).
        """
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
        report, tracked_prs = await pr_agent.run(state.tracked_prs, head_branch=head_branch)
        state.tracked_prs = tracked_prs

        summary.prs_monitored = report.monitored
        summary.prs_merged = len(report.merged)
        summary.prs_escalated = len(report.escalated)
        summary.errors.extend(report.errors)
        summary.prs_fix_requested = len(report.fix_requested)

    async def _run_issue_agent(self, state: OrchestratorState, summary: RunSummary) -> None:
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

    async def _run_upgrade_agent(self, state: OrchestratorState, summary: RunSummary) -> None:
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
        event_payload: dict[str, Any] | None = None,
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
        event_payload: dict[str, Any] | None = None,
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

    async def _run_security_agent(self, state: OrchestratorState, summary: RunSummary) -> None:
        """Run the security agent to triage Dependabot/code-scanning/secret-scanning alerts."""
        cfg = self._config.security_agent
        if not cfg.enabled:
            logger.info("Security agent is disabled")
            return

        if self._config.orchestrator.dry_run:
            logger.info("[DRY RUN] Security agent would run")
            return

        agent = SecurityAgent(
            github=self._github,
            owner=self._owner,
            repo=self._repo,
            min_severity=cfg.min_severity,
            max_issues_per_run=cfg.max_issues_per_run,
            false_positive_rules=cfg.false_positive_rules,
            include_dependabot=cfg.include_dependabot,
            include_code_scanning=cfg.include_code_scanning,
            include_secret_scanning=cfg.include_secret_scanning,
        )
        report = await agent.run()

        summary.security_findings_found = report.findings_found
        summary.security_issues_created = len(report.issues_created)
        summary.security_false_positives = report.false_positives_flagged
        summary.errors.extend(report.errors)

    async def _run_dependency_agent(self, state: OrchestratorState, summary: RunSummary) -> None:
        """Run the dependency agent to handle Dependabot upgrade PRs."""
        cfg = self._config.dependency_agent
        if not cfg.enabled:
            logger.info("Dependency agent is disabled")
            return

        if self._config.orchestrator.dry_run:
            logger.info("[DRY RUN] Dependency agent would run")
            return

        agent = DependencyAgent(
            github=self._github,
            owner=self._owner,
            repo=self._repo,
            auto_merge_patch=cfg.auto_merge_patch,
            auto_merge_minor=cfg.auto_merge_minor,
            merge_method=cfg.merge_method,
            post_digest=cfg.post_digest,
        )
        report = await agent.run()

        summary.dependency_prs_reviewed = report.prs_reviewed
        summary.dependency_prs_auto_merged = len(report.prs_auto_merged)
        summary.dependency_major_issues = len(report.major_issues_created)
        summary.errors.extend(report.errors)

    async def _run_docs_agent(self, state: OrchestratorState, summary: RunSummary) -> None:
        """Run the docs agent to produce weekly CHANGELOG update PRs."""
        cfg = self._config.docs_agent
        if not cfg.enabled:
            logger.info("Docs agent is disabled")
            return

        if self._config.orchestrator.dry_run:
            logger.info("[DRY RUN] Docs agent would run")
            return

        repo_info = await self._github.get_repo(self._owner, self._repo)
        agent = DocsAgent(
            github=self._github,
            owner=self._owner,
            repo=self._repo,
            default_branch=repo_info.default_branch,
            lookback_days=cfg.lookback_days,
            changelog_path=cfg.changelog_path,
            update_readme=cfg.update_readme,
        )
        report = await agent.run()

        summary.docs_prs_analyzed = report.prs_analyzed
        summary.docs_pr_opened = report.doc_pr_opened
        summary.errors.extend(report.errors)

    async def _run_stale_agent(self, state: OrchestratorState, summary: RunSummary) -> None:
        """Run the stale agent to warn/close stale issues/PRs and prune branches."""
        cfg = self._config.stale_agent
        if not cfg.enabled:
            logger.info("Stale agent is disabled")
            return

        if self._config.orchestrator.dry_run:
            logger.info("[DRY RUN] Stale agent would run")
            return

        agent = StaleAgent(
            github=self._github,
            owner=self._owner,
            repo=self._repo,
            stale_days=cfg.stale_days,
            close_after=cfg.close_after,
            close_stale_prs=cfg.close_stale_prs,
            delete_merged_branches=cfg.delete_merged_branches,
            exempt_labels=list(cfg.exempt_labels),
        )
        report = await agent.run()

        summary.stale_issues_warned = report.issues_warned
        summary.stale_issues_closed = report.issues_closed
        summary.stale_branches_deleted = report.branches_deleted
        summary.errors.extend(report.errors)

    async def _run_escalation_agent(self, state: OrchestratorState, summary: RunSummary) -> None:
        """Run the escalation agent to post a human-action-required digest."""
        cfg = self._config.human_escalation
        if not cfg.enabled:
            logger.info("Escalation agent is disabled")
            return

        if self._config.orchestrator.dry_run:
            logger.info("[DRY RUN] Escalation agent would run")
            return

        agent = EscalationAgent(
            github=self._github,
            owner=self._owner,
            repo=self._repo,
            notify_assignees=cfg.notify_assignees,
        )
        report = await agent.run()

        summary.escalation_items_found = report.items_found
        summary.escalation_digest_issue = report.digest_issue_number
        summary.errors.extend(report.errors)
