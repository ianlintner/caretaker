"""Test Agent — monitors test coverage and quality.

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


class TestAgent(BaseAgent):
    """Test coverage and quality agent — placeholder, not yet implemented."""

    @property
    def name(self) -> str:
        return "test"

    def enabled(self) -> bool:
        return self._ctx.config.test_agent.enabled

    async def execute(
        self,
        state: OrchestratorState,
        event_payload: dict[str, Any] | None = None,
    ) -> AgentResult:
        logger.info("TestAgent is a placeholder and does not run yet")
        return AgentResult(processed=0)

    def apply_summary(self, result: AgentResult, summary: RunSummary) -> None:
        summary.test_prs_analyzed = result.extra.get("prs_analyzed", 0)
        summary.test_skeletons_generated = result.extra.get("skeletons_generated", 0)
        summary.test_flaky_detected = result.extra.get("flaky_detected", 0)
