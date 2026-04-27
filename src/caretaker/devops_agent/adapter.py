"""BaseAgent adapter for the CI failure triage agent."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from caretaker.agent_protocol import AgentResult, BaseAgent
from caretaker.devops_agent.agent import DevOpsAgent

if TYPE_CHECKING:
    from caretaker.state.models import OrchestratorState, RunSummary


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
            known_sigs=set(state.reported_build_sigs),
            cooldown_hours=cfg.cooldown_hours,
            issue_cooldowns=state.issue_cooldowns,
        )
        report = await agent.run(event_payload=event_payload)
        # Persist actioned sigs so closed/resolved issues don't spawn duplicates
        if report.actioned_sigs:
            existing = list(state.reported_build_sigs)
            known_sigs = set(existing)
            for sig in report.actioned_sigs:
                if sig not in known_sigs:
                    existing.append(sig)
                    known_sigs.add(sig)
            # Cap at 500 entries to avoid unbounded growth
            state.reported_build_sigs = existing[-500:]
        # Persist updated cooldowns
        state.issue_cooldowns.update(report.updated_cooldowns)
        return AgentResult(
            processed=report.failures_detected,
            errors=report.errors,
            extra={
                "issues_created": report.issues_created,
                "pr_comments_posted": report.pr_comments_posted,
            },
        )

    def apply_summary(self, result: AgentResult, summary: RunSummary) -> None:
        summary.build_failures_detected = result.processed
        summary.build_fix_issues_created = len(result.extra.get("issues_created", []))
        summary.build_fix_pr_comments_posted = len(result.extra.get("pr_comments_posted", []))
