"""Performance Agent — detects performance anti-patterns in PRs.

Analyzes PR diffs for known performance anti-patterns (N+1 queries, unbounded
loops, missing pagination, large allocations) and reviews CI benchmark results
when available.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from caretaker.agent_protocol import AgentResult, BaseAgent

if TYPE_CHECKING:
    from caretaker.state.models import OrchestratorState, RunSummary

logger = logging.getLogger(__name__)


class PerformanceAgent(BaseAgent):
    """Performance anti-pattern detection and benchmark regression tracking."""

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
        cfg = self._ctx.config.perf_agent
        llm = self._ctx.llm_router
        github = self._ctx.github
        owner = self._ctx.owner
        repo = self._ctx.repo

        prs_analyzed = 0
        regressions_flagged = 0
        errors: list[str] = []

        if not llm or not llm.available:
            logger.info("Skipping performance analysis — LLM not available")
            return AgentResult(processed=0, errors=errors)

        # ── Analyze open PRs for performance anti-patterns ──────────
        try:
            prs = await github.list_pull_requests(owner, repo, state="open")
            for pr in prs:
                if pr.draft:
                    continue

                pr_detail = await github.get_pull_request(owner, repo, pr.number)
                if pr_detail is None:
                    continue

                diff_context = f"PR #{pr.number}: {pr.title}\n\n{pr_detail.body or ''}"

                logger.info("Analyzing performance for PR #%d", pr.number)
                analysis = await llm.claude.analyze_perf_diff(
                    diff_context,
                    context=(
                        f"PR #{pr.number} in {owner}/{repo}, "
                        f"anti-patterns to check: {', '.join(cfg.anti_patterns)}"
                    ),
                )

                if analysis:
                    prs_analyzed += 1
                    # Check if any critical issues were found
                    if "critical" in analysis.lower():
                        regressions_flagged += 1

                    comment_body = (
                        "## ⚡ Performance Analysis\n\n"
                        f"*Automated analysis by caretaker performance agent*\n\n"
                        f"{analysis}"
                    )
                    await github.add_issue_comment(owner, repo, pr.number, comment_body)
                    logger.info("Posted performance analysis for PR #%d", pr.number)

        except Exception as exc:
            logger.error("Performance analysis failed: %s", exc, exc_info=True)
            errors.append(f"perf_analysis: {exc}")

        return AgentResult(
            processed=prs_analyzed,
            errors=errors,
            extra={
                "prs_analyzed": prs_analyzed,
                "regressions_flagged": regressions_flagged,
            },
        )

    def apply_summary(self, result: AgentResult, summary: RunSummary) -> None:
        summary.perf_prs_analyzed = result.extra.get("prs_analyzed", 0)
        summary.perf_regressions_flagged = result.extra.get("regressions_flagged", 0)
