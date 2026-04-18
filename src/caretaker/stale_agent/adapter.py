"""BaseAgent adapter for the stale issue/PR/branch cleanup agent."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from caretaker.agent_protocol import AgentResult, BaseAgent
from caretaker.stale_agent.agent import StaleAgent

if TYPE_CHECKING:
    from caretaker.state.models import OrchestratorState, RunSummary


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
