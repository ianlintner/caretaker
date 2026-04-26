"""Tests for the hierarchical graph wiring (Repo → Issue/PR → Run/Comment/CausalEvent).

Covers the relationship work that closed out the "knowledge graph nodes are
disconnected" bug:

* Every per-tenant node is anchored to its owning ``:Repo`` via ``BELONGS_TO``.
* ``CausalEvent.ref`` is lifted into ``ON`` edges
  (CausalEvent → PR / Issue / Comment).
* Marker-bearing comments materialise as ``:Comment`` nodes attached to
  their parent thread (PR/Issue) and to the ``:CausalEvent`` they emit.
* Live workflow runs (parsed from ``run-<id>-<source>`` causal ids) get
  their own ``:Run`` nodes joined to every causal event they emitted via
  ``HAS_EVENT``.

Uses the same ``RecordingStore`` fake pattern as the M2/M3 suites — no
Neo4j dependency in the test path.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from caretaker.admin.causal_store import CausalEventStore
from caretaker.causal_chain import CausalEvent, CausalEventRef
from caretaker.goals.models import GoalSnapshot
from caretaker.graph.builder import GraphBuilder
from caretaker.graph.models import NodeType, RelType
from caretaker.state.models import (
    IssueTrackingState,
    OrchestratorState,
    OwnershipState,
    PRTrackingState,
    RunSummary,
    TrackedIssue,
    TrackedPR,
)


class RecordingStore:
    """Fake :class:`GraphStore` — captures every merge for assertions."""

    def __init__(self) -> None:
        self.nodes: list[tuple[str, str, dict[str, Any]]] = []
        self.edges: list[tuple[str, str, str, str, str, dict[str, Any]]] = []
        self.indexes_ensured = False

    async def ensure_indexes(self) -> None:
        self.indexes_ensured = True

    async def merge_node(self, label: str, node_id: str, props: dict[str, Any]) -> None:
        self.nodes.append((label, node_id, props))

    async def merge_edge(
        self,
        source_label: str,
        source_id: str,
        target_label: str,
        target_id: str,
        rel_type: str,
        properties: dict[str, Any] | None = None,
    ) -> None:
        self.edges.append(
            (
                source_label,
                source_id,
                target_label,
                target_id,
                rel_type,
                properties or {},
            )
        )


_EdgeTuple = tuple[str, str, str, str, str, dict[str, Any]]


def _edges_of(store: RecordingStore, rel: str) -> list[_EdgeTuple]:
    return [e for e in store.edges if e[4] == rel]


def _nodes_of(store: RecordingStore, label: str) -> list[tuple[str, str, dict[str, Any]]]:
    return [n for n in store.nodes if n[0] == label]


def _has_edge(
    store: RecordingStore,
    *,
    source_label: str,
    source_id: str,
    target_label: str,
    target_id: str,
    rel: str,
) -> bool:
    return any(
        e[0] == source_label
        and e[1] == source_id
        and e[2] == target_label
        and e[3] == target_id
        and e[4] == rel
        for e in store.edges
    )


def _sample_state() -> OrchestratorState:
    """A state with one tracked PR, one tracked issue, and one run."""
    state = OrchestratorState()
    state.tracked_prs[42] = TrackedPR(
        number=42,
        state=PRTrackingState.CI_PASSING,
        ownership_state=OwnershipState.OWNED,
        owned_by="copilot",
        ownership_acquired_at=datetime(2026, 4, 20, 11, 30, tzinfo=UTC),
    )
    state.tracked_issues[7] = TrackedIssue(
        number=7,
        state=IssueTrackingState.PR_OPENED,
        assigned_pr=42,
    )
    state.goal_history["ci_health"] = [
        GoalSnapshot(goal_id="ci_health", score=0.9, status="satisfied"),
    ]
    state.run_history.append(
        RunSummary(
            run_at=datetime(2026, 4, 20, 12, 0, tzinfo=UTC),
            mode="pr",
            prs_merged=1,
            goal_health=0.87,
            escalation_rate=0.1,
        )
    )
    return state


def _causal_store_with_events() -> CausalEventStore:
    """Build a causal store with three events spanning all three ref kinds.

    * ``run-9001-pr-agent:escalation`` → ON the PR.
    * ``run-9001-issue-agent:dispatch`` → ON a comment that lives on the
      issue (and Comment → ON → Issue, EMITS → CausalEvent).
    * ``run-9002-charlie:close-stale-issue`` → ON the issue (different
      live run id so we exercise the per-run materialisation).
    """
    store = CausalEventStore()
    now = datetime(2026, 4, 20, 12, 5, tzinfo=UTC)

    pr_event = CausalEvent(
        id="run-9001-pr-agent:escalation",
        source="pr-agent:escalation",
        parent_id=None,
        ref=CausalEventRef(kind="pr", number=42, owner="acme", repo="widgets"),
        run_id="9001",
        title="PR #42 needs escalation",
        observed_at=now,
    )
    comment_event = CausalEvent(
        id="run-9001-issue-agent:dispatch",
        source="issue-agent:dispatch",
        parent_id="run-9001-pr-agent:escalation",
        ref=CausalEventRef(
            kind="comment",
            number=7,
            comment_id=555,
            owner="acme",
            repo="widgets",
        ),
        run_id="9001",
        title="Dispatched issue agent",
        observed_at=now,
    )
    second_run_event = CausalEvent(
        id="run-9002-charlie:close-stale-issue",
        source="charlie:close-stale-issue",
        parent_id=None,
        ref=CausalEventRef(kind="issue", number=7, owner="acme", repo="widgets"),
        run_id="9002",
        title="Charlie closed stale issue",
        observed_at=now,
    )
    store.ingest(pr_event)
    store.ingest(comment_event)
    store.ingest(second_run_event)
    return store


# ── BELONGS_TO scoping ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_belongs_to_anchors_pr_issue_run_to_repo() -> None:
    """PR / Issue / Run nodes each get a ``BELONGS_TO`` edge to their :Repo."""
    store = RecordingStore()
    await GraphBuilder(store).full_sync(_sample_state(), repo="acme/widgets")

    repo_id = "repo:acme/widgets"
    belongs_to = _edges_of(store, RelType.BELONGS_TO.value)
    pairs = {(e[0], e[1]) for e in belongs_to}

    assert (NodeType.PR, "pr:42") in pairs
    assert (NodeType.ISSUE, "issue:7") in pairs
    # Run id is keyed off the run_at isoformat in section 6.
    assert (NodeType.RUN, "run:2026-04-20T12:00:00+00:00") in pairs
    # Goals (synthetic + real) are anchored too.
    assert (NodeType.GOAL, "goal:overall") in pairs
    assert (NodeType.GOAL, "goal:ci_health") in pairs

    # Every BELONGS_TO must terminate at the tenant repo node.
    for _src_label, _src_id, tgt_label, tgt_id, _rel, _props in belongs_to:
        assert tgt_label == NodeType.REPO
        assert tgt_id == repo_id


@pytest.mark.asyncio
async def test_belongs_to_anchors_executor_to_repo() -> None:
    """Executor nodes (copilot/foundry/claude_code) are tenant-scoped too."""
    store = RecordingStore()
    await GraphBuilder(store).full_sync(_sample_state(), repo="acme/widgets")

    assert _has_edge(
        store,
        source_label=NodeType.EXECUTOR,
        source_id="executor:copilot",
        target_label=NodeType.REPO,
        target_id="repo:acme/widgets",
        rel=RelType.BELONGS_TO.value,
    )


@pytest.mark.asyncio
async def test_belongs_to_carries_bitemporal_props() -> None:
    """Every BELONGS_TO edge carries observed_at + valid_from (M2 bitemporal)."""
    store = RecordingStore()
    await GraphBuilder(store).full_sync(_sample_state(), repo="acme/widgets")

    for _src_label, _src_id, _tgt_label, _tgt_id, _rel, props in _edges_of(
        store, RelType.BELONGS_TO.value
    ):
        assert "observed_at" in props
        assert "valid_from" in props


# ── CausalEvent → ON → PR / Issue ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_causal_event_on_pr_edge_when_ref_is_pr() -> None:
    """``ref.kind == 'pr'`` materialises CausalEvent-ON-PR for tracked PRs."""
    record = RecordingStore()
    await GraphBuilder(record).full_sync(
        _sample_state(),
        causal_store=_causal_store_with_events(),
        repo="acme/widgets",
    )

    assert _has_edge(
        record,
        source_label=NodeType.CAUSAL_EVENT,
        source_id="causal:run-9001-pr-agent:escalation",
        target_label=NodeType.PR,
        target_id="pr:42",
        rel=RelType.ON.value,
    )


@pytest.mark.asyncio
async def test_causal_event_on_issue_edge_when_ref_is_issue() -> None:
    """``ref.kind == 'issue'`` materialises CausalEvent-ON-Issue for tracked issues."""
    record = RecordingStore()
    await GraphBuilder(record).full_sync(
        _sample_state(),
        causal_store=_causal_store_with_events(),
        repo="acme/widgets",
    )

    assert _has_edge(
        record,
        source_label=NodeType.CAUSAL_EVENT,
        source_id="causal:run-9002-charlie:close-stale-issue",
        target_label=NodeType.ISSUE,
        target_id="issue:7",
        rel=RelType.ON.value,
    )


@pytest.mark.asyncio
async def test_causal_event_on_pr_skipped_when_pr_not_tracked() -> None:
    """ON edges only land for refs that match a tracked PR/Issue.

    Without this guard the builder would invent dangling edges to PR
    nodes that were never merged (the unique-id constraint would still
    succeed because ``MATCH ... MATCH`` no-ops on missing endpoints,
    but the resulting graph would lie about which PRs caretaker tracks).
    """
    causal = CausalEventStore()
    causal.ingest(
        CausalEvent(
            id="run-9999-pr-agent:escalation",
            source="pr-agent:escalation",
            parent_id=None,
            ref=CausalEventRef(kind="pr", number=8675309, owner="acme", repo="widgets"),
            run_id="9999",
            title="phantom PR",
            observed_at=datetime(2026, 4, 20, 12, 0, tzinfo=UTC),
        )
    )

    record = RecordingStore()
    await GraphBuilder(record).full_sync(_sample_state(), causal_store=causal, repo="acme/widgets")

    on_edges = [
        e
        for e in _edges_of(record, RelType.ON.value)
        if e[2] == NodeType.PR and e[3] == "pr:8675309"
    ]
    assert on_edges == []


# ── Comment node materialisation + EMITS / ON wiring ──────────────────────


@pytest.mark.asyncio
async def test_comment_node_is_materialised_for_comment_ref() -> None:
    """A causal event with ``ref.kind == 'comment'`` mints a :Comment node."""
    record = RecordingStore()
    await GraphBuilder(record).full_sync(
        _sample_state(),
        causal_store=_causal_store_with_events(),
        repo="acme/widgets",
    )

    comments = _nodes_of(record, NodeType.COMMENT.value)
    ids = {n[1] for n in comments}
    assert "comment:7:555" in ids

    # The Comment node carries the parent thread number, the comment id,
    # and the tenant scope.
    _, _, props = next(n for n in comments if n[1] == "comment:7:555")
    assert props["thread_number"] == 7
    assert props["comment_id"] == 555
    assert props["repo"] == "acme/widgets"


@pytest.mark.asyncio
async def test_comment_emits_causal_event_and_on_issue() -> None:
    """Comment-EMITS-CausalEvent and Comment-ON-Issue both land."""
    record = RecordingStore()
    await GraphBuilder(record).full_sync(
        _sample_state(),
        causal_store=_causal_store_with_events(),
        repo="acme/widgets",
    )

    # The marker lives on the comment; the comment "emits" the causal event.
    assert _has_edge(
        record,
        source_label=NodeType.COMMENT,
        source_id="comment:7:555",
        target_label=NodeType.CAUSAL_EVENT,
        target_id="causal:run-9001-issue-agent:dispatch",
        rel=RelType.EMITS.value,
    )
    # The causal event references the comment via ON.
    assert _has_edge(
        record,
        source_label=NodeType.CAUSAL_EVENT,
        source_id="causal:run-9001-issue-agent:dispatch",
        target_label=NodeType.COMMENT,
        target_id="comment:7:555",
        rel=RelType.ON.value,
    )
    # And the comment is attached to its parent issue thread.
    assert _has_edge(
        record,
        source_label=NodeType.COMMENT,
        source_id="comment:7:555",
        target_label=NodeType.ISSUE,
        target_id="issue:7",
        rel=RelType.ON.value,
    )


@pytest.mark.asyncio
async def test_comment_on_pr_when_thread_is_a_tracked_pr() -> None:
    """Comments observed on a PR thread attach to the PR, not the issue."""
    causal = CausalEventStore()
    causal.ingest(
        CausalEvent(
            id="run-9001-pr-agent-task",
            source="pr-agent-task",
            parent_id=None,
            ref=CausalEventRef(
                kind="comment",
                number=42,
                comment_id=999,
                owner="acme",
                repo="widgets",
            ),
            run_id="9001",
            title="task comment",
            observed_at=datetime(2026, 4, 20, 12, 0, tzinfo=UTC),
        )
    )

    record = RecordingStore()
    await GraphBuilder(record).full_sync(_sample_state(), causal_store=causal, repo="acme/widgets")

    assert _has_edge(
        record,
        source_label=NodeType.COMMENT,
        source_id="comment:42:999",
        target_label=NodeType.PR,
        target_id="pr:42",
        rel=RelType.ON.value,
    )
    # And the comment is anchored to the tenant.
    assert _has_edge(
        record,
        source_label=NodeType.COMMENT,
        source_id="comment:42:999",
        target_label=NodeType.REPO,
        target_id="repo:acme/widgets",
        rel=RelType.BELONGS_TO.value,
    )


# ── Live :Run node + HAS_EVENT wiring ─────────────────────────────────────


@pytest.mark.asyncio
async def test_live_run_node_minted_per_workflow_id() -> None:
    """Each unique parsed ``run_id`` yields one ``run:gh:<id>`` :Run node."""
    record = RecordingStore()
    await GraphBuilder(record).full_sync(
        _sample_state(),
        causal_store=_causal_store_with_events(),
        repo="acme/widgets",
    )

    live_runs = {n[1] for n in _nodes_of(record, NodeType.RUN.value) if n[1].startswith("run:gh:")}
    assert live_runs == {"run:gh:9001", "run:gh:9002"}


@pytest.mark.asyncio
async def test_run_has_event_links_workflow_run_to_causal_events() -> None:
    """Every causal event with a parsed run_id is joined to its live :Run."""
    record = RecordingStore()
    await GraphBuilder(record).full_sync(
        _sample_state(),
        causal_store=_causal_store_with_events(),
        repo="acme/widgets",
    )

    has_event = _edges_of(record, RelType.HAS_EVENT.value)
    pairs = {(e[1], e[3]) for e in has_event}

    # Run 9001 emitted two events (the PR escalation and the dispatch comment).
    assert ("run:gh:9001", "causal:run-9001-pr-agent:escalation") in pairs
    assert ("run:gh:9001", "causal:run-9001-issue-agent:dispatch") in pairs
    # Run 9002 emitted the charlie close.
    assert ("run:gh:9002", "causal:run-9002-charlie:close-stale-issue") in pairs


@pytest.mark.asyncio
async def test_caused_by_chain_still_emitted() -> None:
    """The original CAUSED_BY chain is preserved — refactor must not regress it."""
    record = RecordingStore()
    await GraphBuilder(record).full_sync(
        _sample_state(),
        causal_store=_causal_store_with_events(),
        repo="acme/widgets",
    )

    assert _has_edge(
        record,
        source_label=NodeType.CAUSAL_EVENT,
        source_id="causal:run-9001-issue-agent:dispatch",
        target_label=NodeType.CAUSAL_EVENT,
        target_id="causal:run-9001-pr-agent:escalation",
        rel=RelType.CAUSED_BY.value,
    )


# ── Counts ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_full_sync_returns_comments_count() -> None:
    """The returned counts dict exposes the new ``comments`` tally."""
    record = RecordingStore()
    counts = await GraphBuilder(record).full_sync(
        _sample_state(),
        causal_store=_causal_store_with_events(),
        repo="acme/widgets",
    )

    assert counts["comments"] == 1  # only the issue-agent dispatch comment
    assert counts["causal_events"] == 3
