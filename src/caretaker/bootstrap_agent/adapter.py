"""BaseAgent adapter for the BootstrapAgent.

Wires the agent into the registry so the dispatcher can route
``installation`` and ``installation_repositories`` webhooks to it.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

from caretaker.agent_protocol import AgentResult, BaseAgent
from caretaker.bootstrap_agent.agent import BootstrapAgent

if TYPE_CHECKING:
    from caretaker.state.models import OrchestratorState, RunSummary


class BootstrapAgentAdapter(BaseAgent):
    """Adapter wiring :class:`BootstrapAgent` into the agent registry."""

    @property
    def name(self) -> str:
        return "bootstrap"

    def enabled(self) -> bool:
        # Always enabled when present — the agent is a no-op for events
        # that aren't installation-related, and skips repos that already
        # have a bootstrap marker. There's no per-repo opt-out config
        # because the agent only runs on event-driven webhooks.
        return os.environ.get("CARETAKER_BOOTSTRAP_DISABLED", "").lower() not in (
            "1",
            "true",
            "yes",
        )

    async def execute(
        self,
        state: OrchestratorState,
        event_payload: dict[str, Any] | None = None,
    ) -> AgentResult:
        agent = BootstrapAgent(
            github=self._ctx.github,
            owner=self._ctx.owner,
            repo=self._ctx.repo,
            dry_run=self._ctx.dry_run,
        )
        report = await agent.run(event_payload=event_payload)
        return AgentResult(
            processed=len(report.repos_attempted),
            errors=report.errors,
            extra={
                "prs_opened": report.prs_opened,
                "prs_skipped_existing": report.prs_skipped_existing,
            },
        )

    def apply_summary(self, result: AgentResult, summary: RunSummary) -> None:
        # The bootstrap agent's results are operational metadata rather
        # than fleet-health metrics, so we don't fold them into RunSummary
        # counters. The PRs are visible in the run log / admin SPA via
        # ``result.extra``.
        return None
