"""BaseAgent adapter for the PR monitoring agent."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from caretaker.agent_protocol import AgentResult, BaseAgent
from caretaker.pr_agent.agent import PRAgent

if TYPE_CHECKING:
    from caretaker.memory.retriever import MemoryRetriever
    from caretaker.state.models import OrchestratorState, RunSummary

logger = logging.getLogger(__name__)


def _build_memory_retriever(ctx: Any) -> MemoryRetriever | None:
    """Construct a :class:`MemoryRetriever` when the T-E2 gates are open.

    Returns ``None`` whenever any of the following is true:

    * ``config.memory_store.retrieval_enabled`` is false.
    * The readiness agentic mode is ``off`` — no LLM candidate runs, so
      there's no prompt to inject into.
    * The Neo4j driver package is not installed or the graph store
      cannot be constructed.

    When the retriever can't be built we log once at ``info`` level and
    fall through cleanly — the readiness LLM candidate still runs, just
    without cross-run memory context.
    """
    config = ctx.config
    if not config.memory_store.retrieval_enabled:
        return None
    mode = config.agentic.readiness.mode
    if mode not in ("shadow", "enforce"):
        return None
    try:
        from caretaker.graph.store import GraphStore
        from caretaker.memory.embeddings import LiteLLMEmbedder
        from caretaker.memory.retriever import MemoryRetriever
    except ImportError as exc:  # pragma: no cover - optional dep path
        logger.info("T-E2 memory retriever unavailable (import error): %s", exc)
        return None

    try:
        store = GraphStore()
    except Exception as exc:  # noqa: BLE001 - neo4j driver may be absent
        logger.info("T-E2 memory retriever: GraphStore unavailable (%s); skipping", exc)
        return None

    embedder = LiteLLMEmbedder.from_config(config.llm)
    return MemoryRetriever(
        graph=store,
        embedder=embedder if embedder.available else None,
    )


class PRAgentAdapter(BaseAgent):
    """Adapter for the PR monitoring agent."""

    @property
    def name(self) -> str:
        return "pr"

    def enabled(self) -> bool:
        return self._ctx.config.pr_agent.enabled

    async def execute(
        self,
        state: OrchestratorState,
        event_payload: dict[str, Any] | None = None,
    ) -> AgentResult:
        agent = PRAgent(
            github=self._ctx.github,
            owner=self._ctx.owner,
            repo=self._ctx.repo,
            config=self._ctx.config.pr_agent,
            llm_router=self._ctx.llm_router,
            dispatcher=self._ctx.executor_dispatcher,
            memory_retriever=_build_memory_retriever(self._ctx),
            app_id=self._ctx.config.github_app.app_id,
        )
        head_branch: str | None = None
        pr_number: int | None = None
        if event_payload:
            head_branch = event_payload.get("_head_branch")
            pr_number = event_payload.get("_pr_number")
        report, tracked_prs = await agent.run(
            state.tracked_prs, head_branch=head_branch, pr_number=pr_number
        )
        state.tracked_prs = tracked_prs
        return AgentResult(
            processed=report.monitored,
            errors=report.errors,
            extra={
                "merged": report.merged,
                "escalated": report.escalated,
                "fix_requested": report.fix_requested,
            },
        )

    def apply_summary(self, result: AgentResult, summary: RunSummary) -> None:
        summary.prs_monitored = result.processed
        summary.prs_merged = len(result.extra.get("merged", []))
        summary.prs_escalated = len(result.extra.get("escalated", []))
        summary.prs_fix_requested = len(result.extra.get("fix_requested", []))
