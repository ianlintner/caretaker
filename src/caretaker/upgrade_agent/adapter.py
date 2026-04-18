"""BaseAgent adapter for the self-upgrade agent."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from caretaker import __version__
from caretaker.agent_protocol import AgentResult, BaseAgent
from caretaker.upgrade_agent.agent import UpgradeAgent

if TYPE_CHECKING:
    from caretaker.state.models import OrchestratorState, RunSummary


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
            dispatcher=self._ctx.executor_dispatcher,
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
