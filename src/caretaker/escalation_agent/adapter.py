"""BaseAgent adapter for the human-escalation digest agent."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from caretaker.agent_protocol import AgentResult, BaseAgent
from caretaker.escalation_agent.agent import EscalationAgent

if TYPE_CHECKING:
    from caretaker.state.models import OrchestratorState, RunSummary


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
