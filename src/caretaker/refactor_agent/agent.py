"""Refactor Agent — identifies code smells and creates refactoring PRs.

Scans the codebase for code smells (duplication, long functions, dead code),
creates refactoring plans, and can autonomously create PRs for mechanical
improvements via the Foundry executor.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from caretaker.agent_protocol import AgentResult, BaseAgent

if TYPE_CHECKING:
    from caretaker.state.models import OrchestratorState, RunSummary

logger = logging.getLogger(__name__)


class RefactorAgent(BaseAgent):
    """Code smell detection and automated refactoring agent."""

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
        cfg = self._ctx.config.refactor_agent
        llm = self._ctx.llm_router
        github = self._ctx.github
        owner = self._ctx.owner
        repo = self._ctx.repo

        smells_found = 0
        prs_created = 0
        errors: list[str] = []

        if not llm or not llm.available:
            logger.info("Skipping refactor analysis — LLM not available")
            return AgentResult(processed=0, errors=errors)

        # ── Scan recently changed files for code smells ─────────────
        try:
            # Analyze open PRs for code quality issues
            prs = await github.list_pull_requests(owner, repo, state="open")
            for pr in prs:
                if pr.draft:
                    continue

                pr_detail = await github.get_pull_request(owner, repo, pr.number)
                if pr_detail is None:
                    continue

                code_context = f"PR #{pr.number}: {pr.title}\n\n{pr_detail.body or ''}"

                logger.info("Scanning PR #%d for code smells", pr.number)
                analysis = await llm.claude.analyze_code_smells(
                    code_context,
                    context=f"PR #{pr.number} in {owner}/{repo}, "
                    f"target patterns: {', '.join(cfg.target_patterns)}",
                )

                if analysis:
                    smells_found += 1
                    comment_body = (
                        "## 🔍 Code Quality Analysis\n\n"
                        f"*Automated analysis by caretaker refactor agent*\n\n"
                        f"{analysis}"
                    )
                    await github.add_issue_comment(owner, repo, pr.number, comment_body)
                    logger.info("Posted code quality analysis for PR #%d", pr.number)

                    # Create refactoring PR if enabled and confident
                    if (
                        cfg.auto_create_prs
                        and prs_created < cfg.max_prs_per_run
                        and self._ctx.executor_dispatcher
                    ):
                        plan = await llm.claude.plan_refactor(
                            analysis,
                            context=f"{owner}/{repo}",
                        )
                        if plan:
                            # Create an issue describing the refactoring plan
                            issue_body = (
                                "## Refactoring Plan\n\n"
                                f"*Identified by caretaker refactor agent from PR #{pr.number}*\n\n"
                                f"{plan}"
                            )
                            await github.create_issue(
                                owner,
                                repo,
                                title=f"refactor: improvements identified in PR #{pr.number}",
                                body=issue_body,
                                labels=["refactoring", "caretaker"],
                            )
                            prs_created += 1

        except Exception as exc:
            logger.error("Refactor analysis failed: %s", exc, exc_info=True)
            errors.append(f"refactor_analysis: {exc}")

        return AgentResult(
            processed=smells_found,
            errors=errors,
            extra={
                "smells_found": smells_found,
                "prs_created": prs_created,
            },
        )

    def apply_summary(self, result: AgentResult, summary: RunSummary) -> None:
        summary.refactor_smells_found = result.extra.get("smells_found", 0)
        summary.refactor_prs_created = result.extra.get("prs_created", 0)
