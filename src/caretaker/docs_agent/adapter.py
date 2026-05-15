"""BaseAgent adapter for the documentation reconciliation agent."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from caretaker.agent_protocol import AgentResult, BaseAgent
from caretaker.docs_agent.agent import DocsAgent

if TYPE_CHECKING:
    from caretaker.state.models import OrchestratorState, RunSummary


class DocsAgentAdapter(BaseAgent):
    """Adapter for the documentation reconciliation agent."""

    @property
    def name(self) -> str:
        return "docs"

    def enabled(self) -> bool:
        return self._ctx.config.docs_agent.enabled

    async def execute(
        self,
        state: OrchestratorState,
        event_payload: dict[str, Any] | None = None,
    ) -> AgentResult:
        cfg = self._ctx.config.docs_agent
        repo_info = await self._ctx.github.get_repo(self._ctx.owner, self._ctx.repo)

        # For push events, only run when the push is to the default branch.
        # This fires when PRs merge into main but skips the docs agent's own
        # commits to docs/changelog-* branches (which would cause a loop).
        if event_payload is not None and "ref" in event_payload:
            pushed_ref = event_payload.get("ref", "")
            default_ref = f"refs/heads/{repo_info.default_branch}"
            if pushed_ref != default_ref:
                return AgentResult(processed=0, errors=[])
        agent = DocsAgent(
            github=self._ctx.github,
            owner=self._ctx.owner,
            repo=self._ctx.repo,
            default_branch=repo_info.default_branch,
            lookback_days=cfg.lookback_days,
            changelog_path=cfg.changelog_path,
            update_readme=cfg.update_readme,
        )
        report = await agent.run()
        return AgentResult(
            processed=report.prs_analyzed,
            errors=report.errors,
            extra={"doc_pr_opened": report.doc_pr_opened},
        )

    def apply_summary(self, result: AgentResult, summary: RunSummary) -> None:
        summary.docs_prs_analyzed = result.processed
        summary.docs_pr_opened = result.extra.get("doc_pr_opened")
