"""BaseAgent adapter for the shepherd pass.

Runs an end-to-end cleanup loop over open PRs that codifies the manual
shepherd workflow used to triage a backlog:

* inventory + enrich mergeStateStatus (Delta A)
* dedupe CVE / package-bump duplicates (reuses pr_triage)
* promote green Copilot drafts to ready-for-review
* rebase BEHIND PRs via update-branch (Delta C)
* close stale DIRTY drafts the StaleAgent won't touch (Delta C)
* budgeted LLM escalation for still-stuck PRs (Delta F)

Phases 4 (mechanical fixers) / 7 (merge chain) still surface as
``pending-delta-*`` skip tags until their dedicated deltas land.

The agent is always registered, but disabled by default via
``ShepherdConfig.enabled``. It only runs in the dedicated ``shepherd``
mode (not ``full``) so it never silently changes behavior of existing
scheduled runs.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from caretaker.agent_protocol import AgentResult, BaseAgent
from caretaker.pr_agent.shepherd import run_shepherd

if TYPE_CHECKING:
    from caretaker.state.models import OrchestratorState, RunSummary

logger = logging.getLogger(__name__)


class ShepherdAgentAdapter(BaseAgent):
    """Adapter that wires ``run_shepherd`` into the agent registry."""

    @property
    def name(self) -> str:
        return "shepherd"

    def enabled(self) -> bool:
        return self._ctx.config.shepherd.enabled

    async def execute(
        self,
        state: OrchestratorState,
        event_payload: dict[str, Any] | None = None,
    ) -> AgentResult:
        cfg = self._ctx.config.shepherd

        # Pull the Claude client off the router if one is available. When
        # LLMs are disabled entirely, ``run_shepherd`` sees ``claude=None``
        # and emits the ``llm_escalation:no-claude`` skip tag.
        claude = None
        router = self._ctx.llm_router
        if router is not None and getattr(router, "claude_available", False):
            claude = router.claude

        report = await run_shepherd(
            self._ctx.github,
            self._ctx.owner,
            self._ctx.repo,
            cfg,
            dry_run=self._ctx.dry_run or None,
            claude=claude,
        )
        return AgentResult(
            processed=report.action_count,
            errors=list(report.errors),
            extra={
                "inventoried": report.inventoried,
                "enriched": report.enriched,
                "closed_duplicate": list(report.closed_duplicate),
                "promoted": list(report.promoted),
                "mechanical_fixed": list(report.mechanical_fixed),
                "rebased": list(report.rebased),
                "closed_stale": list(report.closed_stale),
                "merged": list(report.merged),
                "llm_budget_used": report.llm_budget_used,
                "llm_verdicts": list(report.llm_verdicts),
                "skipped_phases": list(report.skipped_phases),
            },
        )

    def apply_summary(self, result: AgentResult, summary: RunSummary) -> None:
        # Fold into existing counters — don't add new RunSummary fields yet.
        summary.prs_merged += len(result.extra.get("merged", []))
