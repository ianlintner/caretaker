"""Performance Agent — detects performance anti-patterns in PRs.

PLACEHOLDER: not yet implemented. The LLM analysis workflow is not wired up.
``enabled`` defaults to ``False`` in config; do not enable until this is built.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from caretaker.agent_protocol import AgentResult, BaseAgent

if TYPE_CHECKING:
    from caretaker.state.models import OrchestratorState, RunSummary

logger = logging.getLogger(__name__)


class PerformanceAgent(BaseAgent):
    """Performance anti-pattern detection — placeholder, not yet implemented."""

    @property
    def name(self) -> str:
        return "perf"

    def enabled(self) -> bool:
        return self._ctx.config.perf_agent.enabled

    async def execute(
        self,
        state: OrchestratorState,
        event_payload: dict[str, Any] | None = None,
    ) -> AgentResult:
        logger.info("PerformanceAgent is a placeholder and does not run yet")
        return AgentResult(processed=0)

    def apply_summary(self, result: AgentResult, summary: RunSummary) -> None:
        summary.perf_prs_analyzed = result.extra.get("prs_analyzed", 0)
        summary.perf_regressions_flagged = result.extra.get("regressions_flagged", 0)
