"""BaseAgent adapter for the caretaker self-diagnosis agent."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from caretaker.agent_protocol import AgentResult, BaseAgent
from caretaker.self_heal_agent.agent import SelfHealAgent

if TYPE_CHECKING:
    from caretaker.state.models import OrchestratorState, RunSummary


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
            known_sigs=set(state.reported_self_heal_sigs),
            cooldown_hours=cfg.cooldown_hours,
            issue_cooldowns=state.issue_cooldowns,
        )
        report = await agent.run(event_payload=event_payload)
        # Persist actioned sigs so closed/resolved issues don't spawn duplicates
        if report.actioned_sigs:
            existing = list(state.reported_self_heal_sigs)
            known_sigs = set(existing)
            for sig in report.actioned_sigs:
                if sig not in known_sigs:
                    existing.append(sig)
                    known_sigs.add(sig)
            # Cap at 500 entries to avoid unbounded growth
            state.reported_self_heal_sigs = existing[-500:]
        # Persist updated cooldowns
        state.issue_cooldowns.update(report.updated_cooldowns)
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
