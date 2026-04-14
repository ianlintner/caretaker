"""Adapter layer — wraps legacy agent classes into BaseAgent subclasses."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from caretaker import __version__
from caretaker.agent_protocol import AgentContext, AgentResult, BaseAgent
from caretaker.charlie_agent.agent import CharlieAgent
from caretaker.dependency_agent.agent import DependencyAgent
from caretaker.devops_agent.agent import DevOpsAgent
from caretaker.docs_agent.agent import DocsAgent
from caretaker.escalation_agent.agent import EscalationAgent
from caretaker.issue_agent.agent import IssueAgent
from caretaker.pr_agent.agent import PRAgent
from caretaker.security_agent.agent import SecurityAgent
from caretaker.self_heal_agent.agent import SelfHealAgent
from caretaker.stale_agent.agent import StaleAgent
from caretaker.upgrade_agent.agent import UpgradeAgent

if TYPE_CHECKING:
    from caretaker.registry import AgentRegistry
    from caretaker.state.models import OrchestratorState, RunSummary


# ── PR Agent ──────────────────────────────────────────────────


class PRAgentAdapter(BaseAgent):
    """Adapter for the PR monitoring agent."""

    @property
    def name(self) -> str:
        return "pr"

    def enabled(self) -> bool:
        return self._ctx.config.pr_agent.enabled

    async def execute(
        self,
        state: OrchestratorState,
        event_payload: dict[str, Any] | None = None,
    ) -> AgentResult:
        agent = PRAgent(
            github=self._ctx.github,
            owner=self._ctx.owner,
            repo=self._ctx.repo,
            config=self._ctx.config.pr_agent,
            llm_router=self._ctx.llm_router,
        )
        head_branch: str | None = None
        if event_payload:
            head_branch = event_payload.get("_head_branch")
        report, tracked_prs = await agent.run(state.tracked_prs, head_branch=head_branch)
        state.tracked_prs = tracked_prs
        return AgentResult(
            processed=report.monitored,
            errors=report.errors,
            extra={
                "merged": report.merged,
                "escalated": report.escalated,
                "fix_requested": report.fix_requested,
            },
        )

    def apply_summary(self, result: AgentResult, summary: RunSummary) -> None:
        summary.prs_monitored = result.processed
        summary.prs_merged = len(result.extra.get("merged", []))
        summary.prs_escalated = len(result.extra.get("escalated", []))
        summary.prs_fix_requested = len(result.extra.get("fix_requested", []))


# ── Issue Agent ───────────────────────────────────────────────


class IssueAgentAdapter(BaseAgent):
    """Adapter for the issue triage agent."""

    @property
    def name(self) -> str:
        return "issue"

    def enabled(self) -> bool:
        return self._ctx.config.issue_agent.enabled

    async def execute(
        self,
        state: OrchestratorState,
        event_payload: dict[str, Any] | None = None,
    ) -> AgentResult:
        agent = IssueAgent(
            github=self._ctx.github,
            owner=self._ctx.owner,
            repo=self._ctx.repo,
            config=self._ctx.config.issue_agent,
            llm_router=self._ctx.llm_router,
        )
        report, tracked_issues = await agent.run(state.tracked_issues)
        state.tracked_issues = tracked_issues
        return AgentResult(
            processed=report.triaged,
            errors=report.errors,
            extra={
                "assigned": report.assigned,
                "closed": report.closed,
                "escalated": report.escalated,
            },
        )

    def apply_summary(self, result: AgentResult, summary: RunSummary) -> None:
        summary.issues_triaged = result.processed
        summary.issues_assigned = len(result.extra.get("assigned", []))
        summary.issues_closed = len(result.extra.get("closed", []))
        summary.issues_escalated = len(result.extra.get("escalated", []))


# ── Upgrade Agent ────────────────────────────────────────────


class UpgradeAgentAdapter(BaseAgent):
    """Adapter for the self-upgrade agent."""

    @property
    def name(self) -> str:
        return "upgrade"

    def enabled(self) -> bool:
        return self._ctx.config.upgrade_agent.enabled

    async def execute(
        self,
        state: OrchestratorState,
        event_payload: dict[str, Any] | None = None,
    ) -> AgentResult:
        agent = UpgradeAgent(
            github=self._ctx.github,
            owner=self._ctx.owner,
            repo=self._ctx.repo,
            config=self._ctx.config.upgrade_agent,
            current_version=__version__,
        )
        report = await agent.run()
        return AgentResult(
            processed=1 if report.checked else 0,
            errors=report.errors,
            extra={
                "upgrade_needed": report.upgrade_needed,
                "latest_version": report.latest_version,
            },
        )

    def apply_summary(self, result: AgentResult, summary: RunSummary) -> None:
        summary.upgrade_available = result.extra.get("upgrade_needed", False)
        summary.upgrade_version = result.extra.get("latest_version") or ""


# ── DevOps Agent ─────────────────────────────────────────────


class DevOpsAgentAdapter(BaseAgent):
    """Adapter for the CI failure triage agent."""

    @property
    def name(self) -> str:
        return "devops"

    def enabled(self) -> bool:
        return self._ctx.config.devops_agent.enabled

    async def execute(
        self,
        state: OrchestratorState,
        event_payload: dict[str, Any] | None = None,
    ) -> AgentResult:
        cfg = self._ctx.config.devops_agent
        agent = DevOpsAgent(
            github=self._ctx.github,
            owner=self._ctx.owner,
            repo=self._ctx.repo,
            default_branch=cfg.target_branch,
            max_issues_per_run=cfg.max_issues_per_run,
        )
        report = await agent.run(event_payload=event_payload)
        return AgentResult(
            processed=report.failures_detected,
            errors=report.errors,
            extra={"issues_created": report.issues_created},
        )

    def apply_summary(self, result: AgentResult, summary: RunSummary) -> None:
        summary.build_failures_detected = result.processed
        summary.build_fix_issues_created = len(result.extra.get("issues_created", []))


# ── Self-Heal Agent ──────────────────────────────────────────


class SelfHealAgentAdapter(BaseAgent):
    """Adapter for the caretaker self-diagnosis agent."""

    @property
    def name(self) -> str:
        return "self-heal"

    def enabled(self) -> bool:
        return self._ctx.config.self_heal_agent.enabled

    async def execute(
        self,
        state: OrchestratorState,
        event_payload: dict[str, Any] | None = None,
    ) -> AgentResult:
        cfg = self._ctx.config.self_heal_agent
        report_upstream = cfg.report_upstream and not cfg.is_upstream_repo
        agent = SelfHealAgent(
            github=self._ctx.github,
            owner=self._ctx.owner,
            repo=self._ctx.repo,
            report_upstream=report_upstream,
        )
        report = await agent.run(event_payload=event_payload)
        return AgentResult(
            processed=report.failures_analyzed,
            errors=report.errors,
            extra={
                "local_issues_created": report.local_issues_created,
                "upstream_issues_opened": report.upstream_issues_opened,
                "upstream_features_requested": report.upstream_features_requested,
            },
        )

    def apply_summary(self, result: AgentResult, summary: RunSummary) -> None:
        summary.self_heal_failures_analyzed = result.processed
        summary.self_heal_local_issues = len(result.extra.get("local_issues_created", []))
        summary.self_heal_upstream_bugs = len(result.extra.get("upstream_issues_opened", []))
        summary.self_heal_upstream_features = len(
            result.extra.get("upstream_features_requested", [])
        )


# ── Security Agent ───────────────────────────────────────────


class SecurityAgentAdapter(BaseAgent):
    """Adapter for the security alert triage agent."""

    @property
    def name(self) -> str:
        return "security"

    def enabled(self) -> bool:
        return self._ctx.config.security_agent.enabled

    async def execute(
        self,
        state: OrchestratorState,
        event_payload: dict[str, Any] | None = None,
    ) -> AgentResult:
        cfg = self._ctx.config.security_agent
        agent = SecurityAgent(
            github=self._ctx.github,
            owner=self._ctx.owner,
            repo=self._ctx.repo,
            min_severity=cfg.min_severity,
            max_issues_per_run=cfg.max_issues_per_run,
            false_positive_rules=cfg.false_positive_rules,
            include_dependabot=cfg.include_dependabot,
            include_code_scanning=cfg.include_code_scanning,
            include_secret_scanning=cfg.include_secret_scanning,
        )
        report = await agent.run()
        return AgentResult(
            processed=report.findings_found,
            errors=report.errors,
            extra={
                "issues_created": report.issues_created,
                "false_positives_flagged": report.false_positives_flagged,
            },
        )

    def apply_summary(self, result: AgentResult, summary: RunSummary) -> None:
        summary.security_findings_found = result.processed
        summary.security_issues_created = len(result.extra.get("issues_created", []))
        summary.security_false_positives = result.extra.get("false_positives_flagged", 0)


# ── Dependency Agent ─────────────────────────────────────────


class DependencyAgentAdapter(BaseAgent):
    """Adapter for the Dependabot PR management agent."""

    @property
    def name(self) -> str:
        return "deps"

    def enabled(self) -> bool:
        return self._ctx.config.dependency_agent.enabled

    async def execute(
        self,
        state: OrchestratorState,
        event_payload: dict[str, Any] | None = None,
    ) -> AgentResult:
        cfg = self._ctx.config.dependency_agent
        agent = DependencyAgent(
            github=self._ctx.github,
            owner=self._ctx.owner,
            repo=self._ctx.repo,
            auto_merge_patch=cfg.auto_merge_patch,
            auto_merge_minor=cfg.auto_merge_minor,
            merge_method=cfg.merge_method,
            post_digest=cfg.post_digest,
        )
        report = await agent.run()
        return AgentResult(
            processed=report.prs_reviewed,
            errors=report.errors,
            extra={
                "prs_auto_merged": report.prs_auto_merged,
                "major_issues_created": report.major_issues_created,
            },
        )

    def apply_summary(self, result: AgentResult, summary: RunSummary) -> None:
        summary.dependency_prs_reviewed = result.processed
        summary.dependency_prs_auto_merged = len(result.extra.get("prs_auto_merged", []))
        summary.dependency_major_issues = len(result.extra.get("major_issues_created", []))


# ── Docs Agent ───────────────────────────────────────────────


class DocsAgentAdapter(BaseAgent):
    """Adapter for the documentation reconciliation agent."""

    @property
    def name(self) -> str:
        return "docs"

    def enabled(self) -> bool:
        return self._ctx.config.docs_agent.enabled

    async def execute(
        self,
        state: OrchestratorState,
        event_payload: dict[str, Any] | None = None,
    ) -> AgentResult:
        cfg = self._ctx.config.docs_agent
        repo_info = await self._ctx.github.get_repo(self._ctx.owner, self._ctx.repo)
        agent = DocsAgent(
            github=self._ctx.github,
            owner=self._ctx.owner,
            repo=self._ctx.repo,
            default_branch=repo_info.default_branch,
            lookback_days=cfg.lookback_days,
            changelog_path=cfg.changelog_path,
            update_readme=cfg.update_readme,
        )
        report = await agent.run()
        return AgentResult(
            processed=report.prs_analyzed,
            errors=report.errors,
            extra={"doc_pr_opened": report.doc_pr_opened},
        )

    def apply_summary(self, result: AgentResult, summary: RunSummary) -> None:
        summary.docs_prs_analyzed = result.processed
        summary.docs_pr_opened = result.extra.get("doc_pr_opened")


# ── Charlie Agent ────────────────────────────────────────────


class CharlieAgentAdapter(BaseAgent):
    """Adapter for the janitorial cleanup agent."""

    @property
    def name(self) -> str:
        return "charlie"

    def enabled(self) -> bool:
        return self._ctx.config.charlie_agent.enabled

    async def execute(
        self,
        state: OrchestratorState,
        event_payload: dict[str, Any] | None = None,
    ) -> AgentResult:
        cfg = self._ctx.config.charlie_agent
        agent = CharlieAgent(
            github=self._ctx.github,
            owner=self._ctx.owner,
            repo=self._ctx.repo,
            stale_days=cfg.stale_days,
            close_duplicate_issues=cfg.close_duplicate_issues,
            close_duplicate_prs=cfg.close_duplicate_prs,
            close_stale_issues=cfg.close_stale_issues,
            close_stale_prs=cfg.close_stale_prs,
            exempt_labels=list(cfg.exempt_labels),
        )
        report = await agent.run()
        return AgentResult(
            processed=report.managed_issues_seen + report.managed_prs_seen,
            errors=report.errors,
            extra={
                "managed_issues_seen": report.managed_issues_seen,
                "managed_prs_seen": report.managed_prs_seen,
                "issues_closed": report.issues_closed,
                "prs_closed": report.prs_closed,
                "duplicate_issues_closed": report.duplicate_issues_closed,
                "duplicate_prs_closed": report.duplicate_prs_closed,
            },
        )

    def apply_summary(self, result: AgentResult, summary: RunSummary) -> None:
        summary.charlie_managed_issues = result.extra.get("managed_issues_seen", 0)
        summary.charlie_managed_prs = result.extra.get("managed_prs_seen", 0)
        summary.charlie_issues_closed = result.extra.get("issues_closed", 0)
        summary.charlie_prs_closed = result.extra.get("prs_closed", 0)
        summary.charlie_duplicates_closed = result.extra.get(
            "duplicate_issues_closed", 0
        ) + result.extra.get("duplicate_prs_closed", 0)


# ── Stale Agent ──────────────────────────────────────────────


class StaleAgentAdapter(BaseAgent):
    """Adapter for the stale issue/PR/branch cleanup agent."""

    @property
    def name(self) -> str:
        return "stale"

    def enabled(self) -> bool:
        return self._ctx.config.stale_agent.enabled

    async def execute(
        self,
        state: OrchestratorState,
        event_payload: dict[str, Any] | None = None,
    ) -> AgentResult:
        cfg = self._ctx.config.stale_agent
        agent = StaleAgent(
            github=self._ctx.github,
            owner=self._ctx.owner,
            repo=self._ctx.repo,
            stale_days=cfg.stale_days,
            close_after=cfg.close_after,
            close_stale_prs=cfg.close_stale_prs,
            delete_merged_branches=cfg.delete_merged_branches,
            exempt_labels=list(cfg.exempt_labels),
        )
        report = await agent.run()
        return AgentResult(
            processed=report.issues_warned + report.issues_closed,
            errors=report.errors,
            extra={
                "issues_warned": report.issues_warned,
                "issues_closed": report.issues_closed,
                "branches_deleted": report.branches_deleted,
            },
        )

    def apply_summary(self, result: AgentResult, summary: RunSummary) -> None:
        summary.stale_issues_warned = result.extra.get("issues_warned", 0)
        summary.stale_issues_closed = result.extra.get("issues_closed", 0)
        summary.stale_branches_deleted = result.extra.get("branches_deleted", 0)


# ── Escalation Agent ────────────────────────────────────────


class EscalationAgentAdapter(BaseAgent):
    """Adapter for the human-escalation digest agent."""

    @property
    def name(self) -> str:
        return "escalation"

    def enabled(self) -> bool:
        return self._ctx.config.human_escalation.enabled

    async def execute(
        self,
        state: OrchestratorState,
        event_payload: dict[str, Any] | None = None,
    ) -> AgentResult:
        cfg = self._ctx.config.human_escalation
        agent = EscalationAgent(
            github=self._ctx.github,
            owner=self._ctx.owner,
            repo=self._ctx.repo,
            notify_assignees=cfg.notify_assignees,
        )
        report = await agent.run()
        return AgentResult(
            processed=report.items_found,
            errors=report.errors,
            extra={"digest_issue_number": report.digest_issue_number},
        )

    def apply_summary(self, result: AgentResult, summary: RunSummary) -> None:
        summary.escalation_items_found = result.processed
        summary.escalation_digest_issue = result.extra.get("digest_issue_number")


# ── Factory ──────────────────────────────────────────────────

ALL_ADAPTERS: list[type[BaseAgent]] = [
    PRAgentAdapter,
    IssueAgentAdapter,
    UpgradeAgentAdapter,
    DevOpsAgentAdapter,
    SelfHealAgentAdapter,
    SecurityAgentAdapter,
    DependencyAgentAdapter,
    DocsAgentAdapter,
    CharlieAgentAdapter,
    StaleAgentAdapter,
    EscalationAgentAdapter,
]

# Maps agent name → set of run modes that include it
AGENT_MODES: dict[str, set[str]] = {
    "pr": {"full", "pr-only"},
    "issue": {"full", "issue-only"},
    "upgrade": {"full", "upgrade"},
    "devops": {"full", "devops"},
    "self-heal": {"self-heal"},  # scheduled self-heal mode only
    "security": {"full", "security"},
    "deps": {"full", "deps"},
    "docs": {"full", "docs"},
    "charlie": {"full", "charlie"},
    "stale": {"full", "stale"},
    "escalation": {"full", "escalation"},
}

# Maps GitHub event types → list of agent names to run
EVENT_AGENT_MAP: dict[str, list[str]] = {
    "pull_request": ["pr"],
    "pull_request_review": ["pr"],
    "check_run": ["pr"],
    "check_suite": ["pr"],
    "issues": ["issue"],
    "issue_comment": ["issue"],
    "workflow_run": ["devops", "self-heal", "pr"],
    "dependabot_alert": ["security"],
}


def build_registry(ctx: AgentContext) -> AgentRegistry:
    """Construct a fully populated AgentRegistry from the given context."""
    from caretaker.registry import AgentRegistry

    registry = AgentRegistry()
    for adapter_cls in ALL_ADAPTERS:
        agent = adapter_cls(ctx)
        modes = AGENT_MODES.get(agent.name, {"full"})
        registry.register(agent, modes=modes)
    return registry
