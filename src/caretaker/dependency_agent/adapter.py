"""BaseAgent adapter for the Dependabot PR management agent."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from caretaker.agent_protocol import AgentResult, BaseAgent
from caretaker.dependency_agent.agent import DependencyAgent

if TYPE_CHECKING:
    from caretaker.state.models import OrchestratorState, RunSummary


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
