"""Tests for the 2026-05 graph unification work.

Covers the three pieces added on top of the existing graph stack:

* ``:WebhookKind`` projection — :func:`record_webhook_to_graph` and
  :func:`record_webhook_trigger` enqueue the right node/edge merges.
* :mod:`caretaker.graph.scope` contextvar — push/pop, async-task isolation,
  and the no-op shape when no scope is set.
* ``TOUCHED_MEMORY`` edge wiring on :class:`Neo4jMemoryBackend` — the
  ``_maybe_link_memory_scope`` hook consults the contextvar and pushes
  the expected node + PR/Issue edges through the writer.

All tests use the :class:`FakeGraphStore` pattern from
``test_graph_writer.py`` — no live Neo4j connection is required.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from caretaker.graph.models import NodeType, RelType
from caretaker.graph.scope import Scope, active_scope, current
from caretaker.graph.webhooks import record_webhook_to_graph, record_webhook_trigger
from caretaker.graph.writer import GraphWriter, get_writer, reset_for_tests


class _FakeStore:
    """Records merge_node / merge_edge calls for assertions."""

    def __init__(self) -> None:
        self.nodes: list[tuple[str, str, dict[str, Any]]] = []
        self.edges: list[tuple[str, str, str, str, str, dict[str, Any]]] = []

    async def merge_node(self, label: str, node_id: str, properties: dict[str, Any]) -> None:
        self.nodes.append((label, node_id, properties))

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
            (source_label, source_id, target_label, target_id, rel_type, properties or {})
        )


@pytest.fixture
def writer_with_store() -> tuple[GraphWriter, _FakeStore]:
    """Configured singleton writer + the fake store it writes into."""
    reset_for_tests()
    store = _FakeStore()
    writer = get_writer()
    writer.configure(store)  # type: ignore[arg-type]
    yield writer, store
    reset_for_tests()


# ── :WebhookKind projection ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_record_webhook_to_graph_merges_signature_node_and_repo_anchor(
    writer_with_store: tuple[GraphWriter, _FakeStore],
) -> None:
    writer, store = writer_with_store
    await writer.start()

    record_webhook_to_graph(
        repo="ianlintner/caretaker",
        event_type="pull_request",
        action="opened",
        outcome="active",
    )
    assert await writer.flush(timeout=2.0)
    await writer.stop()

    labels = [n[0] for n in store.nodes]
    # Two nodes get merged: the :WebhookKind aggregate and its :Repo anchor.
    assert NodeType.WEBHOOK_KIND in labels
    assert NodeType.REPO in labels

    kind_node = next(n for n in store.nodes if n[0] == NodeType.WEBHOOK_KIND)
    assert kind_node[1] == "webhook:ianlintner/caretaker:pull_request:opened"
    props = kind_node[2]
    assert props["event_type"] == "pull_request"
    assert props["action"] == "opened"
    assert props["last_outcome"] == "active"
    assert "last_seen" in props

    # The BELONGS_TO edge wires the signature to its repo for subgraph
    # queries scoped on the repo node.
    rels = [e[4] for e in store.edges]
    assert RelType.BELONGS_TO in rels


def test_record_webhook_to_graph_is_noop_when_repo_missing() -> None:
    """No repository_full_name → don't attribute to an empty-string repo."""
    reset_for_tests()
    store = _FakeStore()
    writer = get_writer()
    writer.configure(store)  # type: ignore[arg-type]

    record_webhook_to_graph(repo="", event_type="ping", action=None, outcome="off")

    assert store.nodes == []
    reset_for_tests()


@pytest.mark.asyncio
async def test_record_webhook_trigger_emits_directed_edge(
    writer_with_store: tuple[GraphWriter, _FakeStore],
) -> None:
    writer, store = writer_with_store
    await writer.start()

    record_webhook_trigger(
        repo="ianlintner/caretaker",
        event_type="pull_request",
        action="opened",
        target_label="PR",
        target_id="pr:42",
    )
    assert await writer.flush(timeout=2.0)
    await writer.stop()

    triggered = [e for e in store.edges if e[4] == RelType.TRIGGERED]
    assert len(triggered) == 1
    src_label, src_id, tgt_label, tgt_id, _rel, _props = triggered[0]
    assert (src_label, tgt_label, tgt_id) == (NodeType.WEBHOOK_KIND, "PR", "pr:42")
    assert src_id == "webhook:ianlintner/caretaker:pull_request:opened"


# ── Active scope contextvar ───────────────────────────────────────────────


def test_scope_default_is_none() -> None:
    assert current() is None


def test_scope_push_pop_restores_previous() -> None:
    outer = Scope(repo="o/r", pr_number=1)
    inner = Scope(repo="o/r", issue_number=99)

    assert current() is None
    with active_scope(outer):
        assert current() == outer
        with active_scope(inner):
            assert current() == inner
        assert current() == outer
    assert current() is None


def test_scope_restored_when_block_raises() -> None:
    with pytest.raises(RuntimeError, match="boom"), active_scope(Scope(repo="o/r", pr_number=7)):
        raise RuntimeError("boom")
    assert current() is None


@pytest.mark.asyncio
async def test_scope_is_isolated_across_concurrent_tasks() -> None:
    """Concurrent agent runs scoped to different PRs must not bleed."""

    observed: dict[str, int | None] = {}

    async def run_under(scope: Scope, label: str) -> None:
        with active_scope(scope):
            # Yield so the other task gets a chance to run inside its
            # own scope. If the contextvar leaked, ``current()`` after
            # the sleep would see the *other* task's value.
            await asyncio.sleep(0.01)
            obs = current()
            observed[label] = obs.pr_number if obs else None

    await asyncio.gather(
        run_under(Scope(repo="o/r", pr_number=1), "a"),
        run_under(Scope(repo="o/r", pr_number=2), "b"),
    )
    assert observed == {"a": 1, "b": 2}


# ── TOUCHED_MEMORY wiring via Neo4jMemoryBackend ──────────────────────────


@pytest.mark.asyncio
async def test_maybe_link_memory_scope_emits_pr_edge(
    writer_with_store: tuple[GraphWriter, _FakeStore],
) -> None:
    """When a PR scope is active, set() stamps the PR -[:TOUCHED_MEMORY]-> entry edge."""
    from caretaker.state.backends.neo4j_backend import Neo4jMemoryBackend

    writer, store = writer_with_store
    await writer.start()

    with active_scope(Scope(repo="o/r", pr_number=42)):
        Neo4jMemoryBackend._maybe_link_memory_scope(  # type: ignore[call-arg]
            None,
            "agent:reviewer",
            "last_seen_sha",  # noqa: SLF001
        )

    assert await writer.flush(timeout=2.0)
    await writer.stop()

    mem_nodes = [n for n in store.nodes if n[0] == NodeType.MEMORY_ENTRY]
    assert len(mem_nodes) == 1
    assert mem_nodes[0][1] == "memory:agent:reviewer:last_seen_sha"

    touched = [e for e in store.edges if e[4] == RelType.TOUCHED_MEMORY]
    assert len(touched) == 1
    src_label, src_id, tgt_label, tgt_id, _rel, _props = touched[0]
    assert (src_label, src_id, tgt_label) == ("PR", "pr:42", NodeType.MEMORY_ENTRY)


@pytest.mark.asyncio
async def test_maybe_link_memory_scope_emits_issue_edge(
    writer_with_store: tuple[GraphWriter, _FakeStore],
) -> None:
    from caretaker.state.backends.neo4j_backend import Neo4jMemoryBackend

    writer, store = writer_with_store
    await writer.start()

    with active_scope(Scope(repo="o/r", issue_number=7)):
        Neo4jMemoryBackend._maybe_link_memory_scope(  # type: ignore[call-arg]
            None,
            "ns",
            "k",  # noqa: SLF001
        )

    assert await writer.flush(timeout=2.0)
    await writer.stop()

    touched = [e for e in store.edges if e[4] == RelType.TOUCHED_MEMORY]
    assert len(touched) == 1
    assert touched[0][:3] == ("Issue", "issue:7", NodeType.MEMORY_ENTRY)


def test_maybe_link_memory_scope_noop_without_active_scope() -> None:
    """No PR/Issue scope active → never enqueue a graph write."""
    from caretaker.state.backends.neo4j_backend import Neo4jMemoryBackend

    reset_for_tests()
    store = _FakeStore()
    writer = get_writer()
    writer.configure(store)  # type: ignore[arg-type]

    assert current() is None
    Neo4jMemoryBackend._maybe_link_memory_scope(  # type: ignore[call-arg]
        None,
        "ns",
        "k",  # noqa: SLF001
    )
    assert store.nodes == [] and store.edges == []
    reset_for_tests()


# ── Dispatcher scope extraction ───────────────────────────────────────────


def test_scope_for_extracts_pr_number_from_payload() -> None:
    """The dispatcher helper pulls pull_request.number out of the payload."""
    from caretaker.github_app.dispatcher import _scope_for
    from caretaker.github_app.webhooks import ParsedWebhook

    parsed = ParsedWebhook(
        event_type="pull_request",
        delivery_id="d1",
        action="opened",
        installation_id=1,
        repository_full_name="o/r",
        payload={"pull_request": {"number": 7}},
    )
    with _scope_for(parsed):
        scope = current()
        assert scope is not None
        assert scope.pr_number == 7
        assert scope.repo == "o/r"


def test_trigger_targets_from_pull_request_event() -> None:
    """pull_request.opened → one PR target, no issue target."""
    from caretaker.github_app.dispatcher import _trigger_targets
    from caretaker.github_app.webhooks import ParsedWebhook

    parsed = ParsedWebhook(
        event_type="pull_request",
        delivery_id="d1",
        action="opened",
        installation_id=1,
        repository_full_name="o/r",
        payload={"pull_request": {"number": 42}},
    )
    assert _trigger_targets(parsed) == [("PR", "pr:42")]


def test_trigger_targets_from_issue_comment_on_pr_promotes_to_pr() -> None:
    """issue_comment on a PR carries both — TRIGGERED should point at the PR."""
    from caretaker.github_app.dispatcher import _trigger_targets
    from caretaker.github_app.webhooks import ParsedWebhook

    parsed = ParsedWebhook(
        event_type="issue_comment",
        delivery_id="d2",
        action="created",
        installation_id=1,
        repository_full_name="o/r",
        payload={"issue": {"number": 42, "pull_request": {"url": "..."}}},
    )
    assert _trigger_targets(parsed) == [("PR", "pr:42")]


def test_trigger_targets_from_pure_issue_event() -> None:
    """issues.opened on a non-PR issue → one Issue target only."""
    from caretaker.github_app.dispatcher import _trigger_targets
    from caretaker.github_app.webhooks import ParsedWebhook

    parsed = ParsedWebhook(
        event_type="issues",
        delivery_id="d3",
        action="opened",
        installation_id=1,
        repository_full_name="o/r",
        payload={"issue": {"number": 7}},
    )
    assert _trigger_targets(parsed) == [("Issue", "issue:7")]


def test_scope_for_returns_nullcontext_when_no_pr_or_issue() -> None:
    """Push/ping events have no entity number — helper must be a no-op."""
    from caretaker.github_app.dispatcher import _scope_for
    from caretaker.github_app.webhooks import ParsedWebhook

    parsed = ParsedWebhook(
        event_type="push",
        delivery_id="d1",
        action=None,
        installation_id=1,
        repository_full_name="o/r",
        payload={"ref": "refs/heads/main"},
    )
    with _scope_for(parsed):
        assert current() is None
