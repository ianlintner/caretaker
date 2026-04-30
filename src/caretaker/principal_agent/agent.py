"""Principal Agent — acts as a principal/lead engineer for the repository.

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


class PrincipalAgent(BaseAgent):
    """Principal/lead engineer agent — placeholder, not yet implemented."""

    @property
    def name(self) -> str:
        return "principal"

    def enabled(self) -> bool:
        return self._ctx.config.principal_agent.enabled

    async def execute(
        self,
        state: OrchestratorState,
        event_payload: dict[str, Any] | None = None,
    ) -> AgentResult:
        logger.info("PrincipalAgent is a placeholder and does not run yet")
        return AgentResult(processed=0)

    def apply_summary(self, result: AgentResult, summary: RunSummary) -> None:
        summary.principal_reviews = result.extra.get("reviews", 0)
        summary.principal_prds_created = result.extra.get("prds_created", 0)
        summary.principal_refactors_planned = result.extra.get("refactors_planned", 0)
