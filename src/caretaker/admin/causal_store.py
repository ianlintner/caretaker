"""Persistent store of causal events lifted from GitHub bodies/comments.

Causal events were originally held in an in-memory dict that was *cleared*
on every refresh tick (every 60s). That made the Causal Chain dashboard
unable to walk lineage across runs: closed issues drop out of
``OrchestratorState.tracked_*`` after a few cycles, their bodies stop
being scanned, and any event whose parent lived on one of those bodies
became orphaned. Pod restarts wiped everything entirely.

This module now backs the store with Neo4j (the same graph the rest of
the dashboard already reads from). The in-memory ``OrderedDict`` is kept
only as a fast LRU read-through cache — it is *populated* from Neo4j on
first use and *warmed* by writes, never wiped on refresh. Writes go
through the shared :class:`~caretaker.graph.writer.GraphWriter` so they
survive process death and subsequent ticks.

Memory bounds
-------------
The cache is bounded by ``CARETAKER_CAUSAL_CACHE_MAX_EVENTS`` (default
``5000``). Insertions evict the oldest LRU entry when the bound is
exceeded; warming from Neo4j respects the same cap by ordering rows
``observed_at DESC`` and only loading the newest slice. Lineage walks
that miss the cache fall through to a Neo4j-backed traversal
(:meth:`awalk`, :meth:`adescendants`) so closed-issue events arbitrarily
deep in history remain reachable without pinning them in memory.

When no :class:`~caretaker.graph.store.GraphStore` is wired (unit tests,
local dev without Neo4j) the store still works as a pure in-memory dict
to preserve the contract older callers depend on.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections import OrderedDict
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from caretaker.causal_chain import (
    CausalEvent,
    CausalEventRef,
    Chain,
    descendants,
    extract_all_from_body,
    parse_run_id,
    walk_chain,
)
from caretaker.graph.models import NodeType, RelType
from caretaker.graph.writer import get_writer
from caretaker.state.tracker import TRACKING_ISSUE_TITLE, TRACKING_LABEL

if TYPE_CHECKING:
    from caretaker.github_client.api import GitHubClient
    from caretaker.graph.store import GraphStore
    from caretaker.state.models import OrchestratorState

logger = logging.getLogger(__name__)


DEFAULT_CACHE_MAX_EVENTS = 5000
CACHE_MAX_EVENTS_ENV = "CARETAKER_CAUSAL_CACHE_MAX_EVENTS"


def _resolve_default_cache_cap() -> int:
    """Read the env-configured cap, clamped to a sensible floor of 100."""
    raw = os.environ.get(CACHE_MAX_EVENTS_ENV)
    if not raw:
        return DEFAULT_CACHE_MAX_EVENTS
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "%s=%r is not an int; falling back to %d",
            CACHE_MAX_EVENTS_ENV,
            raw,
            DEFAULT_CACHE_MAX_EVENTS,
        )
        return DEFAULT_CACHE_MAX_EVENTS
    return max(100, value)


def _causal_node_id(event_id: str) -> str:
    """Graph-side id used by the builder for ``:CausalEvent`` nodes."""
    return f"causal:{event_id}"


def _strip_node_prefix(node_id: str) -> str:
    """Inverse of :func:`_causal_node_id`."""
    if node_id.startswith("causal:"):
        return node_id[len("causal:") :]
    return node_id


def _parse_observed_at(raw: Any) -> datetime | None:
    if raw in (None, "", b""):
        return None
    if isinstance(raw, datetime):
        return raw
    if isinstance(raw, str):
        try:
            dt = datetime.fromisoformat(raw)
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    return None


def _row_to_event(row: dict[str, Any]) -> CausalEvent | None:
    """Reconstruct a :class:`CausalEvent` from a graph property dict."""
    raw_id = row.get("id") or row.get("name")
    if not raw_id:
        return None
    event_id = _strip_node_prefix(str(raw_id))
    ref_kind = (row.get("ref_kind") or row.get("kind") or "issue") or "issue"
    ref_number = row.get("ref_number")
    if ref_number is not None:
        try:
            ref_number_int: int | None = int(ref_number)
        except (TypeError, ValueError):
            ref_number_int = None
    else:
        ref_number_int = None
    comment_id = row.get("ref_comment_id")
    if comment_id is not None:
        try:
            comment_id_int: int | None = int(comment_id)
        except (TypeError, ValueError):
            comment_id_int = None
    else:
        comment_id_int = None
    owner = row.get("ref_owner") or row.get("owner") or ""
    repo_slug = row.get("repo") or ""
    repo_name = ""
    if isinstance(repo_slug, str) and "/" in repo_slug:
        owner = owner or repo_slug.split("/", 1)[0]
        repo_name = repo_slug.split("/", 1)[1]
    parent = row.get("parent_id") or None
    if parent == "":
        parent = None
    ref = CausalEventRef(
        kind=str(ref_kind),
        number=ref_number_int,
        comment_id=comment_id_int,
        owner=str(owner or ""),
        repo=str(repo_name or ""),
    )
    return CausalEvent(
        id=event_id,
        source=str(row.get("source") or ""),
        parent_id=str(parent) if parent else None,
        ref=ref,
        run_id=str(row.get("run_id")) if row.get("run_id") else parse_run_id(event_id),
        title=str(row.get("title") or ""),
        observed_at=_parse_observed_at(row.get("observed_at")),
        span_id=row.get("span_id") or None,
        parent_span_id=row.get("parent_span_id") or None,
    )


def _event_node_props(event: CausalEvent, repo_slug: str) -> dict[str, Any]:
    """Property bag the writer / builder stores on a :CausalEvent node."""
    props: dict[str, Any] = {
        "name": event.id,
        "source": event.source,
        "run_id": event.run_id,
        "title": event.title,
        "ref_kind": event.ref.kind,
        "ref_number": event.ref.number,
        "ref_comment_id": event.ref.comment_id,
        "ref_owner": event.ref.owner,
        "repo": repo_slug,
    }
    if event.parent_id:
        props["parent_id"] = event.parent_id
    if event.observed_at:
        props["observed_at"] = event.observed_at.isoformat()
    if event.span_id:
        props["span_id"] = event.span_id
    if event.parent_span_id:
        props["parent_span_id"] = event.parent_span_id
    # Strip None values — GraphWriter is tolerant but the builder treats
    # missing keys differently from explicit None on subsequent merges.
    return {k: v for k, v in props.items() if v is not None}


class CausalEventStore:
    """Indexed collection of :class:`CausalEvent` observed across the repo.

    Backed by Neo4j when a :class:`GraphStore` is wired via
    :meth:`attach_graph_store`; otherwise behaves as a pure in-memory
    LRU dict (used by unit tests and local development).
    """

    def __init__(
        self,
        graph_store: GraphStore | None = None,
        *,
        cache_max_events: int | None = None,
    ) -> None:
        # OrderedDict order tracks LRU. Most-recently used is at the
        # tail; the head is the eviction candidate.
        self._index: OrderedDict[str, CausalEvent] = OrderedDict()
        self._graph_store: GraphStore | None = graph_store
        self._cache_warmed = False
        self._cache_cap = (
            cache_max_events if cache_max_events is not None else _resolve_default_cache_cap()
        )
        # Diagnostic counters surfaced via :meth:`stats`.
        self._neo4j_reads = 0
        self._neo4j_writes = 0
        self._evictions = 0

    # ── Wiring ────────────────────────────────────────────────────────────

    def attach_graph_store(self, store: GraphStore | None) -> None:
        """Wire the persistent backend (idempotent).

        Called from :mod:`caretaker.admin.state_loader` once the shared
        :class:`GraphStore` has been constructed. After this point all
        queries and writes go through Neo4j; the in-memory dict remains
        as a fast cache.
        """
        if store is self._graph_store:
            return
        self._graph_store = store
        self._cache_warmed = False  # force warm on next read

    @property
    def is_persistent(self) -> bool:
        return self._graph_store is not None

    @property
    def cache_cap(self) -> int:
        return self._cache_cap

    # ── Ingestion ─────────────────────────────────────────────────────────

    def clear(self) -> None:
        """Drop the in-memory cache.

        Does **not** clear Neo4j — that's intentional: the persistent
        store survives across refresh ticks and pod restarts. Tests that
        need a clean slate should construct a fresh
        :class:`CausalEventStore` (or use the in-memory mode without a
        :class:`GraphStore`).
        """
        self._index.clear()
        self._cache_warmed = False

    def ingest(self, event: CausalEvent) -> None:
        """Upsert ``event`` into the index.

        Writes go to:

        * The in-memory LRU cache (immediately readable by the same
          process; oldest entry evicted when over the cap).
        * The shared :class:`GraphWriter` queue (durable, asynchronously
          flushed to Neo4j by the writer's drain task).

        Later writes win on duplicate id. The graph-side merge is keyed
        by ``causal:<event.id>`` to match what
        :class:`~caretaker.graph.builder.GraphBuilder` produces.
        """
        if event.id in self._index:
            self._index.move_to_end(event.id)
        self._index[event.id] = event
        self._enforce_cache_cap()
        self._enqueue_event(event)

    def ingest_body(
        self,
        body: str,
        *,
        ref: CausalEventRef,
        title: str = "",
        observed_at: datetime | None = None,
    ) -> CausalEvent | None:
        """Parse + ingest **every** causal marker in ``body``.

        Returns the first event for backwards compatibility with callers
        that only care whether a marker was present. Multi-marker bodies
        (digest comments, orchestrator state comments) used to silently
        drop the trailing markers because the underlying extractor only
        returned the first match — that bug is fixed here.
        """
        events = extract_all_from_body(
            body or "",
            ref=ref,
            title=title,
            observed_at=observed_at,
        )
        if not events:
            return None
        for event in events:
            self.ingest(event)
        return events[0]

    def _enforce_cache_cap(self) -> None:
        """Evict oldest entries until ``len(self._index) <= self._cache_cap``."""
        cap = self._cache_cap
        if cap <= 0:
            return
        while len(self._index) > cap:
            self._index.popitem(last=False)
            self._evictions += 1

    def _enqueue_event(self, event: CausalEvent) -> None:
        """Queue a Neo4j write through the shared :class:`GraphWriter`.

        Best-effort — when the writer is not configured (offline tests,
        local dev) the call is a no-op and only the in-memory cache is
        updated. Writes are asynchronous; downstream readers may need to
        consult Neo4j directly through :meth:`get` / :meth:`walk` to see
        them after the writer has flushed.
        """
        if self._graph_store is None:
            return
        writer = get_writer()
        repo_slug = self._derive_repo_slug(event)
        node_id = _causal_node_id(event.id)
        try:
            writer.record_node(
                NodeType.CAUSAL_EVENT,
                node_id,
                _event_node_props(event, repo_slug),
            )
            if repo_slug:
                writer.record_node(NodeType.REPO, f"repo:{repo_slug}", {"name": repo_slug})
                writer.record_edge(
                    NodeType.CAUSAL_EVENT,
                    node_id,
                    NodeType.REPO,
                    f"repo:{repo_slug}",
                    RelType.BELONGS_TO,
                    {"observed_at": _now_iso()},
                )
            if event.parent_id:
                writer.record_edge(
                    NodeType.CAUSAL_EVENT,
                    node_id,
                    NodeType.CAUSAL_EVENT,
                    _causal_node_id(event.parent_id),
                    RelType.CAUSED_BY,
                    {"observed_at": _now_iso()},
                )
            self._neo4j_writes += 1
        except Exception:  # pragma: no cover - writer is best-effort
            logger.debug("CausalEventStore: graph enqueue failed for %s", event.id, exc_info=True)

    @staticmethod
    def _derive_repo_slug(event: CausalEvent) -> str:
        owner = event.ref.owner or ""
        repo = event.ref.repo or ""
        if owner and repo:
            return f"{owner}/{repo}"
        return ""

    async def refresh_from_github(
        self,
        github: GitHubClient,
        owner: str,
        repo: str,
        state: OrchestratorState,
    ) -> int:
        """Scan tracked PRs/issues + tracking issue comments; return event count.

        Crucially this method **does not** clear the index any more.
        Events are upserted incrementally so causal history survives
        across refresh ticks and pod restarts. Closing an issue or
        deleting a comment no longer destroys the lineage record — the
        graph remains the source of truth.

        Returns the post-refresh size of the in-memory cache (Neo4j may
        hold strictly more events than the cache, since closed issues
        drop out of the scan window).
        """
        # Tracked issues — fetch body + comments.
        for number in list(state.tracked_issues.keys()):
            try:
                issue = await github.get_issue(owner, repo, number)
                if issue is not None:
                    self.ingest_body(
                        issue.body or "",
                        ref=CausalEventRef(kind="issue", number=number, owner=owner, repo=repo),
                        title=issue.title or "",
                        observed_at=getattr(issue, "created_at", None),
                    )
                # Comments: reuse get_pr_comments (same endpoint for issues)
                comments = await github.get_pr_comments(owner, repo, number)
                for c in comments:
                    self.ingest_body(
                        c.body or "",
                        ref=CausalEventRef(
                            kind="comment",
                            number=number,
                            comment_id=c.id,
                            owner=owner,
                            repo=repo,
                        ),
                        observed_at=getattr(c, "created_at", None),
                    )
            except Exception:
                logger.debug("Causal refresh: issue #%d skipped", number, exc_info=True)

        # Tracked PRs — body + comments.
        for number in list(state.tracked_prs.keys()):
            try:
                pr = await github.get_pull_request(owner, repo, number)
                if pr is not None:
                    self.ingest_body(
                        pr.body or "",
                        ref=CausalEventRef(kind="pr", number=number, owner=owner, repo=repo),
                        title=pr.title or "",
                        observed_at=getattr(pr, "created_at", None),
                    )
                comments = await github.get_pr_comments(owner, repo, number)
                for c in comments:
                    self.ingest_body(
                        c.body or "",
                        ref=CausalEventRef(
                            kind="comment",
                            number=number,
                            comment_id=c.id,
                            owner=owner,
                            repo=repo,
                        ),
                        observed_at=getattr(c, "created_at", None),
                    )
            except Exception:
                logger.debug("Causal refresh: PR #%d skipped", number, exc_info=True)

        # Tracking issue comments — state-tracker + run-history markers live here.
        try:
            issues = await github.list_issues(owner, repo, state="open", labels=TRACKING_LABEL)
            for tracker_issue in issues:
                if tracker_issue.title != TRACKING_ISSUE_TITLE:
                    continue
                # Pick up markers stamped on the body itself too — the
                # orchestrator ships a top-level state marker there.
                self.ingest_body(
                    tracker_issue.body or "",
                    ref=CausalEventRef(
                        kind="issue",
                        number=tracker_issue.number,
                        owner=owner,
                        repo=repo,
                    ),
                    title=tracker_issue.title or "",
                    observed_at=getattr(tracker_issue, "created_at", None),
                )
                comments = await github.get_pr_comments(owner, repo, tracker_issue.number)
                for c in comments:
                    self.ingest_body(
                        c.body or "",
                        ref=CausalEventRef(
                            kind="comment",
                            number=tracker_issue.number,
                            comment_id=c.id,
                            owner=owner,
                            repo=repo,
                        ),
                        observed_at=getattr(c, "created_at", None),
                    )
        except Exception:
            logger.debug("Causal refresh: tracking issue scan skipped", exc_info=True)

        # If we have a persistent backend, rehydrate the cache from Neo4j
        # so closed-issue events (no longer in the scan window) come back
        # into the in-memory snapshot the chain walker uses.
        if self._graph_store is not None:
            try:
                await self._warm_cache_from_graph(force=True)
            except Exception:
                logger.debug("Causal refresh: cache warm from graph failed", exc_info=True)

        return len(self._index)

    async def _warm_cache_from_graph(self, *, force: bool = False) -> None:
        """Load the most-recent ``cache_cap`` :CausalEvent rows from Neo4j.

        Order is ``observed_at DESC`` so the cache holds the freshest
        slice — older history stays on disk and is reachable via
        :meth:`aget` / :meth:`awalk` / :meth:`adescendants` when
        traversal asks for it.
        """
        if self._graph_store is None:
            return
        if self._cache_warmed and not force:
            return
        try:
            rows = await self._graph_store.list_nodes_with_properties(
                "CausalEvent",
                order_by="coalesce(n.observed_at, '') DESC",
                limit=self._cache_cap,
            )
        except TypeError:
            # Backwards-compat for fakes that don't implement order_by/limit.
            rows = await self._graph_store.list_nodes_with_properties("CausalEvent")
        self._neo4j_reads += 1
        # Load oldest-first so the most recent ends up at the LRU tail.
        loaded: list[CausalEvent] = []
        for row in rows:
            event = _row_to_event(row)
            if event is not None:
                loaded.append(event)
        # Rows from Neo4j are newest-first; reverse so newest end up most-recent.
        for event in reversed(loaded):
            # setdefault so an in-process write (potentially richer than
            # the persisted copy on a previous tick) wins.
            if event.id not in self._index:
                self._index[event.id] = event
        self._enforce_cache_cap()
        self._cache_warmed = True

    # ── Queries ───────────────────────────────────────────────────────────

    def size(self) -> int:
        return len(self._index)

    def stats(self) -> dict[str, Any]:
        """Diagnostic counters used by the admin dashboard."""
        return {
            "cache_size": len(self._index),
            "cache_cap": self._cache_cap,
            "persistent": self._graph_store is not None,
            "cache_warmed": self._cache_warmed,
            "neo4j_reads": self._neo4j_reads,
            "neo4j_writes": self._neo4j_writes,
            "evictions": self._evictions,
        }

    def get(self, event_id: str) -> CausalEvent | None:
        """Return the cached event; warm from Neo4j if necessary.

        This is intentionally synchronous to keep the public API stable
        for callers that only have access to a sync context (admin REST
        endpoints query through the dict via
        :meth:`AdminDataAccess.get_causal_chain`). When a sync warm is
        needed and the cache is cold we *attempt* a synchronous fetch
        via the running event loop — if there is none we fall through
        to the cached miss.
        """
        cached = self._index.get(event_id)
        if cached is not None:
            self._index.move_to_end(event_id)
            return cached
        self._maybe_sync_warm()
        cached = self._index.get(event_id)
        if cached is not None:
            self._index.move_to_end(event_id)
        return cached

    async def aget(self, event_id: str) -> CausalEvent | None:
        """Async variant of :meth:`get` that always tries the persistent backend."""
        cached = self._index.get(event_id)
        if cached is not None:
            self._index.move_to_end(event_id)
            return cached
        if self._graph_store is None:
            return None
        try:
            rows = await self._graph_store.list_nodes_with_properties(
                "CausalEvent",
                where="n.id = $id",
                params={"id": _causal_node_id(event_id)},
            )
            self._neo4j_reads += 1
        except Exception:
            logger.debug("CausalEventStore: aget failed for %s", event_id, exc_info=True)
            return None
        if not rows:
            return None
        event = _row_to_event(rows[0])
        if event is not None:
            self._index[event.id] = event
            self._enforce_cache_cap()
        return event

    def index(self) -> dict[str, CausalEvent]:
        """Return the in-memory index (warming from Neo4j if cold).

        :class:`~caretaker.graph.builder.GraphBuilder` consumes this
        directly to merge ``:CausalEvent`` nodes during full-sync; warm
        the cache up front so it sees the freshest slice. Note that this
        view is bounded by ``cache_cap`` — older lineage lives only in
        Neo4j and is reached via :meth:`awalk`/:meth:`adescendants`.
        """
        self._maybe_sync_warm()
        return self._index

    def list_events(
        self,
        *,
        source: str | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> tuple[list[CausalEvent], int]:
        self._maybe_sync_warm()
        events = list(self._index.values())
        if source:
            events = [e for e in events if e.source == source]
        # Most recent first (by observed_at, then by id).
        events.sort(
            key=lambda e: (e.observed_at.isoformat() if e.observed_at else "", e.id),
            reverse=True,
        )
        total = len(events)
        return events[offset : offset + limit], total

    def walk(self, event_id: str, *, max_depth: int = 50) -> Chain:
        self._maybe_sync_warm()
        return walk_chain(self._index, event_id, max_depth=max_depth)

    def descendants(self, event_id: str, *, max_depth: int = 50) -> list[CausalEvent]:
        self._maybe_sync_warm()
        return descendants(self._index, event_id, max_depth=max_depth)

    # ── Async (Neo4j-traversal) variants ──────────────────────────────────

    async def awalk(self, event_id: str, *, max_depth: int = 50) -> Chain:
        """Walk ancestors using Neo4j when the cache is incomplete.

        Falls back to the cached :meth:`walk` when no graph store is
        wired. When persistent, traverses ``CAUSED_BY`` edges through
        Cypher and rebuilds events row-by-row so deeply-historical
        chains (whose ancestors are no longer in the LRU cache) still
        return a complete walk.
        """
        if self._graph_store is None:
            return self.walk(event_id, max_depth=max_depth)
        depth = max(1, int(max_depth))
        try:
            rows = await self._graph_store.list_nodes_with_properties(
                "CausalEvent",
                where=(f"n.id = $id OR (n)-[:CAUSED_BY*1..{depth}]-(:CausalEvent {{id: $id}})"),
                params={"id": _causal_node_id(event_id)},
            )
            self._neo4j_reads += 1
        except Exception:
            logger.debug("CausalEventStore: awalk fallback failed for %s", event_id, exc_info=True)
            return self.walk(event_id, max_depth=max_depth)
        snapshot: dict[str, CausalEvent] = {}
        for row in rows:
            event = _row_to_event(row)
            if event is not None:
                snapshot[event.id] = event
        # Merge in any cached extras so we don't lose richness.
        for cached_id, cached_event in self._index.items():
            snapshot.setdefault(cached_id, cached_event)
        return walk_chain(snapshot, event_id, max_depth=max_depth)

    async def adescendants(self, event_id: str, *, max_depth: int = 50) -> list[CausalEvent]:
        """Descendant traversal that consults Neo4j when cache misses."""
        if self._graph_store is None:
            return self.descendants(event_id, max_depth=max_depth)
        depth = max(1, int(max_depth))
        try:
            rows = await self._graph_store.list_nodes_with_properties(
                "CausalEvent",
                where=(f"n.id = $id OR (:CausalEvent {{id: $id}})-[:CAUSED_BY*1..{depth}]-(n)"),
                params={"id": _causal_node_id(event_id)},
            )
            self._neo4j_reads += 1
        except Exception:
            logger.debug(
                "CausalEventStore: adescendants fallback failed for %s", event_id, exc_info=True
            )
            return self.descendants(event_id, max_depth=max_depth)
        snapshot: dict[str, CausalEvent] = {}
        for row in rows:
            event = _row_to_event(row)
            if event is not None:
                snapshot[event.id] = event
        for cached_id, cached_event in self._index.items():
            snapshot.setdefault(cached_id, cached_event)
        return descendants(snapshot, event_id, max_depth=max_depth)

    # ── Internal: synchronous warm ────────────────────────────────────────

    def _maybe_sync_warm(self) -> None:
        """Best-effort cache warm from Neo4j inside a sync entry point.

        The admin REST endpoints are synchronous (they call ``walk`` /
        ``index`` directly) but the underlying driver is async. We try
        to drive the warm coroutine on whatever event loop is available;
        if none is, the call is a no-op and we serve the (possibly
        stale) cache as before. The 60-second refresh task warms the
        cache aggressively so this fallback is rarely the only path.
        """
        if self._graph_store is None or self._cache_warmed:
            return
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            return
        if loop.is_running():
            # Inside a running loop — schedule the warm as a task; the
            # caller will see the cache populated on a subsequent call.
            with contextlib.suppress(RuntimeError):  # pragma: no cover - defensive
                loop.create_task(self._warm_cache_from_graph())
            return
        try:
            loop.run_until_complete(self._warm_cache_from_graph())
        except Exception:  # pragma: no cover - best-effort
            logger.debug("CausalEventStore: sync warm failed", exc_info=True)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


__all__ = ["CausalEventStore", "DEFAULT_CACHE_MAX_EVENTS", "CACHE_MAX_EVENTS_ENV"]
