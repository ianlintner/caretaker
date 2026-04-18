"""BaseAgent adapter for the security alert triage agent."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from caretaker.agent_protocol import AgentResult, BaseAgent
from caretaker.security_agent.agent import SecurityAgent

if TYPE_CHECKING:
    from caretaker.state.models import OrchestratorState, RunSummary


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
