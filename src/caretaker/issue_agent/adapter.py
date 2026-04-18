"""BaseAgent adapter for the issue triage agent."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from caretaker.agent_protocol import AgentResult, BaseAgent
from caretaker.issue_agent.agent import IssueAgent

if TYPE_CHECKING:
    from caretaker.state.models import OrchestratorState, RunSummary


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
            dispatcher=self._ctx.executor_dispatcher,
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
