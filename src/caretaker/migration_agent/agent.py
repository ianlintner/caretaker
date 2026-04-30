"""Migration Agent — manages framework/language version migrations.

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


class MigrationAgent(BaseAgent):
    """Framework/language migration management agent — placeholder, not yet implemented."""

    @property
    def name(self) -> str:
        return "migration"

    def enabled(self) -> bool:
        return self._ctx.config.migration_agent.enabled

    async def execute(
        self,
        state: OrchestratorState,
        event_payload: dict[str, Any] | None = None,
    ) -> AgentResult:
        logger.info("MigrationAgent is a placeholder and does not run yet")
        return AgentResult(processed=0)

    def apply_summary(self, result: AgentResult, summary: RunSummary) -> None:
        summary.migration_deprecations_found = result.extra.get("deprecations_found", 0)
        summary.migration_fixes_applied = result.extra.get("fixes_applied", 0)
