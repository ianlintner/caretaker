"""Refactor Agent — identifies code smells and creates refactoring PRs.

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


class RefactorAgent(BaseAgent):
    """Code smell detection and automated refactoring — placeholder, not yet implemented."""

    @property
    def name(self) -> str:
        return "refactor"

    def enabled(self) -> bool:
        return self._ctx.config.refactor_agent.enabled

    async def execute(
        self,
        state: OrchestratorState,
        event_payload: dict[str, Any] | None = None,
    ) -> AgentResult:
        logger.info("RefactorAgent is a placeholder and does not run yet")
        return AgentResult(processed=0)

    def apply_summary(self, result: AgentResult, summary: RunSummary) -> None:
        summary.refactor_smells_found = result.extra.get("smells_found", 0)
        summary.refactor_prs_created = result.extra.get("prs_created", 0)
