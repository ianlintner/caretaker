"""Agent core-memory publisher — §4.3 of ``docs/memory-graph-plan.md``.

Every agent run publishes one small :class:`AgentCoreMemory` node into
the graph via the process-wide :class:`~caretaker.graph.writer.GraphWriter`.
The payload follows the Letta "core memory block" pattern — identity,
active goal, active run-id, active PR, a short ring of recent-action ids,
and an approximate context-token count — so the admin UI and future
agent-facing memory browser can answer "what was agent X thinking during
run R?" in a single cypher hop.

Design notes
------------

* :func:`publish` is non-blocking. It calls the writer singleton, which
  enqueues the node + edge and drains them on a background task. No
  SQLite or Neo4j IO happens on the dispatch hot path.
* Writes are upsert-only — the writer uses ``MERGE`` semantics, so a
  fresh ``publish`` call for the same ``(agent, run_id)`` tuple
  overwrites the in-place row. We deliberately never delete prior
  memory: the full audit lives in the per-agent SQLite KV store.
* The edge from :class:`AgentCoreMemory` to :class:`~caretaker.graph.models.NodeType.AGENT`
  uses the :data:`~caretaker.graph.models.RelType.CORE_MEMORY_OF`
  relationship so "which agent does this core-memory belong to?" is a
  direct one-hop query.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from caretaker.graph.models import NodeType, RelType
from caretaker.graph.writer import get_writer


@dataclass
class AgentCoreMemory:
    """Per-agent working-memory snapshot emitted once per run.

    Fields added for T-E2 (cross-run memory retrieval):

    * ``summary`` — short human-readable recap of the dispatch; the
      retriever uses this as the Jaccard fallback key and as the text
      surfaced in the prompt injection.
    * ``outcome`` — terminal status tag (``"merged"``, ``"closed_unmerged"``,
      ``"escalated"``, ``"success"``, ``"failure"``, ...). Surfaced
      verbatim in the retrieval bullet so the LLM can weight "like last
      time, but it merged" vs "like last time, and it was escalated".
    * ``pr_number`` / ``issue_number`` — subject attribution, used to
      render "(PR #N)" in the retrieval bullet.
    * ``summary_embedding`` — optional dense vector computed via the
      wired :class:`~caretaker.memory.embeddings.Embedder`. Stored only
      when ``config.memory_store.retrieval_enabled`` is true and an
      embedder is supplied to :func:`publish`; otherwise callers rank
      the Jaccard fallback.
    """

    agent: str
    run_id: str
    repo: str
    identity: str
    active_goal: str | None = None
    active_pr: int | None = None
    recent_action_ids: list[str] = field(default_factory=list)
    context_tokens: int = 0
    # ── T-E2 retrieval fields ────────────────────────────────────────
    summary: str | None = None
    outcome: str | None = None
    pr_number: int | None = None
    issue_number: int | None = None
    summary_embedding: list[float] | None = None


def publish(core: AgentCoreMemory) -> None:
    """Publish ``core`` to the graph via :func:`~caretaker.graph.writer.get_writer`.

    Enqueues one :data:`~caretaker.graph.models.NodeType.AGENT_CORE_MEMORY`
    node plus a :data:`~caretaker.graph.models.RelType.CORE_MEMORY_OF`
    edge to the owning ``:Agent{id=agent:<name>}`` node. Non-blocking,
    safe to call when the writer is disabled (e.g. unit tests without a
    Neo4j cluster) — every ``record_*`` call becomes a cheap no-op.

    The function never awaits and never raises; callers can wire it in
    at the start of each agent dispatch without worrying about hot-path
    latency or error propagation.
    """
    writer = get_writer()
    node_id = f"acm:{core.agent}:{core.run_id}"
    agent_id = f"agent:{core.agent}"

    node_props: dict[str, Any] = {
        "agent": core.agent,
        "run_id": core.run_id,
        # ``run_at`` mirrors ``run_id`` when the id is already an ISO
        # timestamp (the current convention in ``registry.run_one``). The
        # retriever reads this field to order ties by recency.
        "run_at": core.run_id,
        "repo": core.repo,
        "identity": core.identity,
        "active_goal": core.active_goal,
        "active_pr": core.active_pr,
        # Flatten to a comma-separated string — Neo4j accepts primitive
        # arrays but the writer's retry loop is simpler if every prop is
        # a scalar. The ring rarely exceeds ~10 entries so the cost is
        # negligible.
        "recent_action_ids": ",".join(core.recent_action_ids),
        "context_tokens": core.context_tokens,
        # T-E2 retrieval fields. All are optional on the dataclass so
        # publishers that don't opt in leave them as ``None`` and the
        # retriever treats the row as "no retrieval context available".
        "summary": core.summary,
        "outcome": core.outcome,
        "pr_number": core.pr_number,
        "issue_number": core.issue_number,
    }

    if core.summary_embedding:
        # Same flattening rationale as ``recent_action_ids``: store the
        # vector as a comma-separated string of floats so every property
        # stays scalar. The retriever's ``_parse_stored_embedding``
        # round-trips this back to ``list[float]``.
        node_props["summary_embedding"] = ",".join(
            f"{float(v):.6f}" for v in core.summary_embedding
        )

    writer.record_node(NodeType.AGENT_CORE_MEMORY.value, node_id, node_props)
    writer.record_edge(
        source_label=NodeType.AGENT_CORE_MEMORY.value,
        source_id=node_id,
        target_label=NodeType.AGENT.value,
        target_id=agent_id,
        rel_type=RelType.CORE_MEMORY_OF.value,
        properties={"repo": core.repo},
    )


async def publish_with_embedding(
    core: AgentCoreMemory,
    *,
    embedder: Any | None = None,
) -> None:
    """Compute a ``summary_embedding`` when possible, then :func:`publish`.

    This is the T-E2 write-path upgrade: when an
    :class:`~caretaker.memory.embeddings.Embedder` is wired and the
    dispatch has a non-empty ``summary``, we embed the summary and stamp
    the vector onto the node so :class:`MemoryRetriever` can rank by
    cosine similarity next time. If the embedder is absent or
    unavailable the function degrades to a plain :func:`publish` call —
    the retriever's Jaccard fallback still works on the stored summary.

    The function is async because embedders are async, but it never
    raises: every error path (no embedder, unavailable embedder, embed
    call failure) falls through to the non-embedding publish path.
    """
    if core.summary and embedder is not None and getattr(embedder, "available", False):
        try:
            vector = await embedder.embed(core.summary)
        except Exception:  # noqa: BLE001 - write path must never fail the dispatch
            vector = []
        if vector:
            core = replace_embedding(core, vector)
    publish(core)


def replace_embedding(core: AgentCoreMemory, vector: list[float]) -> AgentCoreMemory:
    """Return a copy of ``core`` with ``summary_embedding`` set.

    Kept separate from :func:`publish_with_embedding` so callers that
    already have a vector in hand (e.g. re-embedding in a backfill job)
    can stamp it without touching the async embed path.
    """
    return AgentCoreMemory(
        agent=core.agent,
        run_id=core.run_id,
        repo=core.repo,
        identity=core.identity,
        active_goal=core.active_goal,
        active_pr=core.active_pr,
        recent_action_ids=list(core.recent_action_ids),
        context_tokens=core.context_tokens,
        summary=core.summary,
        outcome=core.outcome,
        pr_number=core.pr_number,
        issue_number=core.issue_number,
        summary_embedding=list(vector),
    )


@dataclass
class IncidentMemory:
    """Wave A3 — one ``:Incident`` row per self-heal dispatch.

    Mirrors :class:`AgentCoreMemory` but scoped to the self-heal
    agent's fix-ladder + escalation path: the ``error_signature`` is
    the 12-char hash produced by
    :func:`caretaker.self_heal_agent.agent._sig`, ``kind`` is the
    coarse :class:`FailureKind`, and ``fix_outcome`` records what
    happened (``fixed`` / ``partial`` / ``escalated`` / ``no_op`` /
    ``error``). ``summary_embedding`` is populated via
    :func:`publish_incident_with_embedding` when an embedder is
    configured.

    Seeds the corpus Wave B3's Neo4j-native vector-index retriever
    will consume. Fail-closed: rows without an embedding still
    persist — the retriever falls back to Jaccard on ``summary``.
    """

    repo: str
    error_signature: str
    kind: str
    job_name: str
    summary: str
    fix_outcome: str
    run_id: str | None = None
    rungs_tried: list[str] = field(default_factory=list)
    summary_embedding: list[float] | None = None


def publish_incident(incident: IncidentMemory) -> None:
    """Enqueue a ``:Incident`` node merge via the shared graph writer.

    Non-blocking — when the writer is disabled (no Neo4j cluster
    configured, or running in unit tests) every ``record_*`` call is
    a cheap no-op. Edges to the owning ``:Repo`` are emitted via
    :data:`~caretaker.graph.models.RelType.REFERENCES` so fleet-level
    queries can answer "which repos are alerting on sig=X?" in one
    hop.
    """
    writer = get_writer()
    # ``incident:<repo>:<signature>`` keeps one row per
    # (repo, signature) — repeated failures of the same signature
    # upsert in place, matching the storm-cap semantics the agent
    # already uses for issue creation.
    safe_sig = incident.error_signature or "unknown"
    node_id = f"incident:{incident.repo}:{safe_sig}"

    props: dict[str, Any] = {
        "repo": incident.repo,
        "error_signature": safe_sig,
        "kind": incident.kind,
        "job_name": incident.job_name,
        "summary": incident.summary,
        "fix_outcome": incident.fix_outcome,
        "run_id": incident.run_id,
        # Comma-joined for the same "every property is a scalar"
        # reason the core-memory writer flattens ``recent_action_ids``.
        "rungs_tried": ",".join(incident.rungs_tried),
    }
    if incident.summary_embedding:
        props["summary_embedding"] = ",".join(f"{float(v):.6f}" for v in incident.summary_embedding)

    writer.record_node(NodeType.INCIDENT.value, node_id, props)
    # One edge per (incident, repo). ``:Repo`` nodes are already
    # emitted by the attribution writer — we don't need to re-merge
    # the target here.
    writer.record_edge(
        source_label=NodeType.INCIDENT.value,
        source_id=node_id,
        target_label=NodeType.REPO.value,
        target_id=f"repo:{incident.repo}",
        rel_type=RelType.REFERENCES.value,
        properties={"repo": incident.repo},
    )


async def publish_incident_with_embedding(
    incident: IncidentMemory,
    *,
    embedder: Any | None = None,
    write_embeddings: bool = False,
) -> None:
    """Compute ``summary_embedding`` when ``write_embeddings`` is enabled.

    Wave A3's seed-the-corpus path. The function only reaches for
    the embedder when ``write_embeddings`` is true AND an embedder
    was supplied — otherwise the row persists without a vector and
    the retriever's Jaccard fallback still works. Every error path
    degrades to :func:`publish_incident` so the write never fails
    the self-heal dispatch.
    """
    if (
        write_embeddings
        and incident.summary
        and embedder is not None
        and getattr(embedder, "available", False)
    ):
        try:
            vector = await embedder.embed(incident.summary)
        except Exception:  # noqa: BLE001 - write path must never fail the dispatch
            vector = []
        if vector:
            incident = IncidentMemory(
                repo=incident.repo,
                error_signature=incident.error_signature,
                kind=incident.kind,
                job_name=incident.job_name,
                summary=incident.summary,
                fix_outcome=incident.fix_outcome,
                run_id=incident.run_id,
                rungs_tried=list(incident.rungs_tried),
                summary_embedding=list(vector),
            )
    publish_incident(incident)


__all__ = [
    "AgentCoreMemory",
    "IncidentMemory",
    "publish",
    "publish_incident",
    "publish_incident_with_embedding",
    "publish_with_embedding",
    "replace_embedding",
]
