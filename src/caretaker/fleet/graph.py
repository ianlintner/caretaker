"""Fleet graph sync + :GlobalSkill promotion (M6 of the memory-graph plan).

Plan reference: ``docs/memory-graph-plan.md`` §5. The fleet registry
already stores per-repo heartbeats (agents enabled, last goal health,
caretaker version) in :class:`caretaker.fleet.store.FleetRegistryStore`.
This module is the bridge that projects that data into the Neo4j graph
so cypher queries can answer fleet-wide questions (which repos run
the PR agent; which repos share a given :GlobalSkill; etc.).

Two entry points:

* :func:`sync_repos_to_graph` — for each known fleet client, merges a
  :Repo node + RUNS_AGENT edges for every enabled agent + a
  GOAL_HEALTH edge to the synthetic ``goal:overall`` node carrying
  ``{score, as_of}`` from the last heartbeat. Idempotent: safe to run
  on every admin refresh tick.

* :func:`promote_global_skills` — scans :Skill nodes grouped by
  ``signature``, promotes signatures seen in ≥ ``min_repos`` distinct
  repos into :GlobalSkill nodes. The per-skill SOP text is run
  through :func:`caretaker.fleet.abstraction.abstract_sop` before it
  lands on the :GlobalSkill node; the raw :Skill nodes themselves are
  untouched. Gated behind ``fleet.share_skills = True`` by the caller.

Design notes
------------

* Both functions take the :class:`~caretaker.graph.store.GraphStore`
  as a thin protocol (they only use ``merge_node`` / ``merge_edge``
  and, for promotion, ``list_skill_rows``). Tests use a recording fake
  store; in production the real Neo4j-backed store is passed through.
* No retries. The singleton :class:`caretaker.graph.writer.GraphWriter`
  is designed for hot-path async writes; these sync paths are called
  from the admin refresh loop which already catches and logs failures
  at the outer layer.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol

from caretaker.evolution.insight_store import Skill
from caretaker.fleet.abstraction import abstract_sop
from caretaker.graph.models import NodeType, RelType

if TYPE_CHECKING:
    from caretaker.evolution.insight_store import InsightStore
    from caretaker.fleet.store import FleetRegistryStore

logger = logging.getLogger(__name__)


class _GraphStoreProtocol(Protocol):
    """Duck-type the subset of :class:`GraphStore` this module needs."""

    async def merge_node(self, label: str, node_id: str, properties: dict[str, Any]) -> None: ...

    async def merge_edge(
        self,
        source_label: str,
        source_id: str,
        target_label: str,
        target_id: str,
        rel_type: str,
        properties: dict[str, Any] | None = None,
    ) -> None: ...

    async def list_skill_rows(self) -> list[dict[str, Any]]: ...


class _GlobalSkillStoreProtocol(Protocol):
    """Async source of ``:GlobalSkill`` rows (a subset of :class:`GraphStore`)."""

    async def list_global_skill_rows(self, category: str | None = None) -> list[dict[str, Any]]: ...


def _repo_node_id(slug: str) -> str:
    return f"repo:{slug}"


def _agent_node_id(name: str) -> str:
    return f"agent:{name}"


def _global_skill_id(signature: str) -> str:
    """Stable id for a GlobalSkill derived from its signature.

    The signature is the cross-repo skill fingerprint; collisions
    would mean two different procedural skills share a name which
    isn't a meaningful concept for the fleet tier. Using the
    signature directly (as opposed to a hash) keeps the node id
    human-readable in the graph UI.
    """
    return f"global_skill:{signature}"


async def sync_repos_to_graph(
    store: _GraphStoreProtocol,
    fleet_store: FleetRegistryStore,
) -> dict[str, int]:
    """Project fleet-registry clients into the graph.

    For each known client:

    * Merge ``:Repo{slug, last_heartbeat_at, caretaker_version}``.
    * For every ``enabled_agents`` entry, merge
      ``(:Repo)-[:RUNS_AGENT]->(:Agent)``.
    * When ``last_goal_health`` is not None, merge
      ``(:Repo)-[:GOAL_HEALTH {score, as_of}]->(:Goal)`` pointing at
      the synthetic ``goal:overall`` aggregate that the builder
      already merges on every full sync. The edge is the one-hop
      answer to "what was repo X's goal health on its last heartbeat"
      without having to scan per-run edges.

    Returns a counters dict for observability. Safe to call when the
    fleet store is empty — returns zeroes.
    """
    counts = {"repos": 0, "runs_agent_edges": 0, "goal_health_edges": 0}

    clients = await fleet_store.list_clients()
    for client in clients:
        repo_id = _repo_node_id(client.repo)
        await store.merge_node(
            NodeType.REPO,
            repo_id,
            {
                "slug": client.repo,
                "repo": client.repo,
                "last_heartbeat_at": client.last_seen.isoformat(),
                "caretaker_version": client.caretaker_version,
            },
        )
        counts["repos"] += 1

        # RUNS_AGENT fan-out. Each heartbeat carries the repo's
        # currently-enabled agent roster, so these edges are the
        # authoritative record of which agents the consumer actually
        # runs today (vs. the statically-defined AGENT_MODES catalog).
        for agent_name in client.enabled_agents:
            await store.merge_edge(
                NodeType.REPO,
                repo_id,
                NodeType.AGENT,
                _agent_node_id(agent_name),
                RelType.RUNS_AGENT,
                {
                    "observed_at": client.last_seen.isoformat(),
                    "valid_from": client.last_seen.isoformat(),
                },
            )
            counts["runs_agent_edges"] += 1

        if client.last_goal_health is not None:
            await store.merge_edge(
                NodeType.REPO,
                repo_id,
                NodeType.GOAL,
                "goal:overall",
                RelType.GOAL_HEALTH,
                {
                    "score": client.last_goal_health,
                    "as_of": client.last_seen.isoformat(),
                    "observed_at": client.last_seen.isoformat(),
                },
            )
            counts["goal_health_edges"] += 1

    logger.debug("Fleet graph sync: %s", counts)
    return counts


async def promote_global_skills(
    store: _GraphStoreProtocol,
    insight_store: InsightStore | None,
    min_repos: int,
    *,
    share_skills: bool = False,
) -> list[str]:
    """Promote per-repo :Skill signatures seen in ≥ N repos to :GlobalSkill.

    Workflow:

    1. Scan all :Skill nodes (``store.list_skill_rows()``) and group by
       ``signature``. Skills without a signature are ignored — they
       can't be matched across repos.
    2. Keep signatures whose distinct-repo count ≥ ``min_repos``.
    3. For each surviving signature, run the SOP text through
       :func:`abstract_sop` (best-effort redactor, §5.4 of the plan).
       The SOP text comes from ``insight_store.all_skills()`` — if
       the insight store is not wired up or has no matching row, the
       signature itself is used as a fallback SOP so the node still
       lands with *some* abstracted description.
    4. Merge ``:GlobalSkill`` + ``PROMOTED_TO`` edges from every
       source :Skill + ``SHARES_SKILL`` edges from every contributing
       :Repo.

    Args:
        store: Graph store implementing ``list_skill_rows`` + merge_*.
        insight_store: Optional per-repo skill store, used for SOP text.
        min_repos: Minimum distinct-repo count for promotion.
        share_skills: Master switch. ``False`` (default) short-circuits
            the function to a no-op; callers must opt in explicitly
            (``fleet.share_skills``).

    Returns:
        The list of promoted signatures. Empty list when disabled or
        no signature clears the gate.

    Privacy contract
    ----------------

    This function **never** writes a :GlobalSkill node without running
    the source SOP text through :func:`abstract_sop` first. The guard
    is intentionally inside the inner loop rather than at the edges so
    a future refactor can't accidentally bypass it. See §5.4 of the
    plan; the redactor is best-effort and the promotion pipeline still
    pairs this with a per-repo opt-in gate (``share_skills``).
    """
    if not share_skills:
        logger.debug("promote_global_skills: fleet.share_skills disabled; no-op")
        return []

    if min_repos < 1:
        # Defensive — zero or negative would promote every single-
        # repo signature which defeats the whole fleet-tier idea.
        logger.warning("promote_global_skills: min_repos=%d is invalid; skipping", min_repos)
        return []

    rows = await store.list_skill_rows()

    # Group by signature → {repo: {sop: str, skill_id: str, row: dict}}.
    # Keep per-repo dedup so multiple Skill rows in the same repo only
    # count once toward the promotion threshold.
    grouped: dict[str, dict[str, dict[str, Any]]] = {}
    for row in rows:
        signature = (row.get("signature") or "").strip()
        if not signature:
            continue
        repo = (row.get("repo") or "").strip()
        if not repo or repo == "unknown/unknown":
            # Untagged data is not eligible — we can't reason about
            # privacy gates without a real tenant.
            continue
        grouped.setdefault(signature, {})[repo] = {
            "skill_id": row.get("id", ""),
            "category": row.get("category", ""),
            "name": row.get("name", ""),
        }

    # Pre-fetch SOP text keyed by signature so the inner loop doesn't
    # pay the all_skills() cost per promotion.
    sop_by_signature: dict[str, str] = {}
    if insight_store is not None:
        try:
            for skill in insight_store.all_skills():
                if skill.sop_text and skill.signature:
                    # Later wins — same signature may appear in both
                    # local and shared backends; either SOP text is
                    # redacted identically.
                    sop_by_signature[skill.signature] = skill.sop_text
        except Exception:  # InsightStore is optional / best-effort
            logger.debug("insight_store.all_skills() failed", exc_info=True)

    now = datetime.now(UTC).isoformat()
    promoted: list[str] = []

    for signature, by_repo in grouped.items():
        repos = list(by_repo.keys())
        if len(repos) < min_repos:
            continue

        # Mandatory abstraction pass. The deny_list includes the set of
        # repos that contributed to this promotion so path scrubbing
        # catches e.g. ``src/acme-widget/main.py`` even when the plain
        # ``acme/widget`` slug isn't in the text.
        raw_sop = sop_by_signature.get(signature, signature)
        deny_list = list(repos) + [r.split("/", 1)[1] for r in repos if "/" in r]
        abstracted = abstract_sop(raw_sop, deny_list=deny_list)

        gs_id = _global_skill_id(signature)
        await store.merge_node(
            NodeType.GLOBAL_SKILL,
            gs_id,
            {
                "name": signature,
                "signature": signature,
                "abstracted_sop_text": abstracted,
                "repo_count": len(repos),
                "abstracted_at": now,
            },
        )

        for repo, skill_meta in by_repo.items():
            skill_node_id = skill_meta["skill_id"]
            if skill_node_id:
                await store.merge_edge(
                    NodeType.SKILL,
                    skill_node_id,
                    NodeType.GLOBAL_SKILL,
                    gs_id,
                    RelType.PROMOTED_TO,
                    {
                        "observed_at": now,
                        "valid_from": now,
                        "confidence": 0.0,  # populated by downstream passes
                    },
                )
            await store.merge_edge(
                NodeType.REPO,
                _repo_node_id(repo),
                NodeType.GLOBAL_SKILL,
                gs_id,
                RelType.SHARES_SKILL,
                {"observed_at": now, "valid_from": now},
            )

        promoted.append(signature)
        logger.info(
            "promote_global_skills: promoted signature=%r repos=%d",
            signature,
            len(repos),
        )

    return promoted


def _row_to_global_skill(row: dict[str, Any]) -> Skill:
    """Adapt a ``:GlobalSkill`` row into a :class:`Skill` tagged as global.

    ``:GlobalSkill`` nodes don't carry per-repo success/fail counters
    — those only make sense at the per-repo tier. We synthesise a
    confidence story from ``repo_count`` instead:

    * ``success_count`` = ``repo_count`` (each contributing repo is one
      cross-repo success signal).
    * ``fail_count`` = 0.
    * ``Skill.confidence`` already requires ``total_attempts >= 3`` to
      return a non-zero value, which lines up with the default
      ``fleet.min_repos_for_promotion = 3`` threshold: a signature
      that just barely cleared promotion starts at confidence 1.0,
      same as a repo-local skill that has only ever succeeded.
    """
    signature = row.get("signature", "")
    repo_count = int(row.get("repo_count", 0) or 0)
    now = datetime.now(UTC)
    return Skill(
        id=row.get("id") or f"global_skill:{signature}",
        category=row.get("category", "") or "",
        signature=signature,
        sop_text=row.get("sop_text", "") or row.get("name", "") or signature,
        success_count=repo_count,
        fail_count=0,
        last_used_at=None,
        created_at=now,
        scope="global",
    )


class GraphBackedGlobalSkillReader:
    """Sync :class:`GlobalSkillReader` impl on top of the async :class:`GraphStore`.

    Bridges the async ``list_global_skill_rows`` call through a
    dedicated background event loop so the sync ``get_relevant`` code
    path (PR agent CI triage, foundry prompt renderer) can surface
    fleet-promoted skills without touching the caller's loop.

    The reader is resilient: any exception propagating out of the
    underlying driver is logged and swallowed. Promotion write-loops
    and prompt builds must not take each other down.
    """

    def __init__(self, store: _GlobalSkillStoreProtocol) -> None:
        self._store = store

    def list_global_skills(self, category: str) -> list[Skill]:
        try:
            rows = self._run(self._store.list_global_skill_rows(category))
        except Exception:  # pragma: no cover — swallowed to protect hot path
            logger.debug("GraphBackedGlobalSkillReader.list_global_skills failed", exc_info=True)
            return []
        return [_row_to_global_skill(row) for row in rows]

    @staticmethod
    def _run(coro: Any) -> Any:
        """Drive *coro* to completion regardless of the caller's loop state.

        Three cases:

        1. No event loop running on this thread → ``asyncio.run``.
        2. A loop is running on this thread (unusual for the sync
           callers but defensive) → spin up a fresh loop in a dedicated
           worker thread so the current loop isn't re-entered.
        3. Same as (2) for safety if ``get_event_loop`` raises.
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            # No loop on this thread — the common case.
            return asyncio.run(coro)

        # A loop is active: run the coro in a worker thread with its own loop.
        result: dict[str, Any] = {}
        error: dict[str, BaseException] = {}

        def _worker() -> None:
            try:
                loop = asyncio.new_event_loop()
                try:
                    result["value"] = loop.run_until_complete(coro)
                finally:
                    loop.close()
            except BaseException as exc:  # noqa: BLE001 — re-raised on join
                error["exc"] = exc

        thread = threading.Thread(target=_worker, daemon=True)
        thread.start()
        thread.join()
        if "exc" in error:
            raise error["exc"]
        return result.get("value")


__all__ = [
    "GraphBackedGlobalSkillReader",
    "promote_global_skills",
    "sync_repos_to_graph",
]
