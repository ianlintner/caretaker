"""BaseAgent adapter for the PR monitoring agent."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from caretaker.agent_protocol import AgentResult, BaseAgent
from caretaker.pr_agent.agent import PRAgent

if TYPE_CHECKING:
    from caretaker.state.models import OrchestratorState, RunSummary


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
            dispatcher=self._ctx.executor_dispatcher,
        )
        head_branch: str | None = None
        pr_number: int | None = None
        if event_payload:
            head_branch = event_payload.get("_head_branch")
            pr_number = event_payload.get("_pr_number")
        report, tracked_prs = await agent.run(
            state.tracked_prs, head_branch=head_branch, pr_number=pr_number
        )
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
