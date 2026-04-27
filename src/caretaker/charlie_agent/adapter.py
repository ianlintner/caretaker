"""BaseAgent adapter for the janitorial cleanup agent."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from caretaker.agent_protocol import AgentResult, BaseAgent
from caretaker.charlie_agent.agent import CharlieAgent

if TYPE_CHECKING:
    from caretaker.state.models import OrchestratorState, RunSummary


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
