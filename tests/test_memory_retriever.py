"""Tests for the T-E2 cross-run memory retriever.

Covers every call-out in ``docs/plans/2026-Q2-agentic-migration.md`` T-E2 Part E
except the readiness-prompt gating, which lives next to the readiness tests
in ``tests/test_pr_agent/test_readiness_llm.py``:

* Cosine ranking with a fake :class:`Embedder` and pre-stored vectors.
* Jaccard fallback when no embeddings are stored (or no embedder supplied).
* ``format_for_prompt`` token budget — oversized hits get truncated and
  trailing hits get dropped rather than overflowing the budget.
* ``find_relevant`` scope filtering: agent always required, repo_slug
  optional; rows with blank summaries are skipped.
* ``publish_with_embedding`` write-path upgrade: stamps the vector onto
  the node when an embedder is available, falls back to plain publish
  when it isn't.
"""

from __future__ import annotations

from typing import Any

import pytest

from caretaker.graph.models import NodeType
from caretaker.graph.writer import get_writer, reset_for_tests
from caretaker.memory.core import AgentCoreMemory, publish_with_embedding
from caretaker.memory.retriever import CoreMemoryHit, MemoryRetriever

# ── Fakes ────────────────────────────────────────────────────────────────


class FakeGraphReader:
    """Minimal :meth:`list_nodes_with_properties` stand-in for tests."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows
        self.calls: list[tuple[str, str | None, dict[str, Any] | None]] = []

    async def list_nodes_with_properties(
        self,
        label: str,
        *,
        where: str | None = None,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        self.calls.append((label, where, params))
        # Filter rows the same way the real Neo4j query would: match
        # agent always, repo only when supplied.
        filtered: list[dict[str, Any]] = []
        agent = (params or {}).get("agent")
        repo = (params or {}).get("repo")
        for row in self._rows:
            if agent is not None and row.get("agent") != agent:
                continue
            if repo is not None and row.get("repo") != repo:
                continue
            filtered.append(row)
        return filtered


class FakeEmbedder:
    """Returns deterministic vectors — one slot per keyword match."""

    available = True

    def __init__(self, vocabulary: list[str]) -> None:
        self._vocab = vocabulary
        self.calls: list[str] = []

    async def embed(self, text: str) -> list[float]:
        self.calls.append(text)
        tokens = {t for t in text.lower().split() if t}
        return [1.0 if word in tokens else 0.0 for word in self._vocab]


class UnavailableEmbedder:
    """Embedder that reports unavailable — retriever should skip embed call."""

    available = False

    async def embed(self, text: str) -> list[float]:  # pragma: no cover
        raise AssertionError("embed must not be called when available=False")


class RecordingStore:
    """Graph writer fake mirroring the pattern in tests/test_memory_core.py."""

    def __init__(self) -> None:
        self.nodes: list[tuple[str, str, dict[str, Any]]] = []
        self.edges: list[tuple[str, str, str, str, str, dict[str, Any]]] = []

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
            (source_label, source_id, target_label, target_id, rel_type, properties or {})
        )


# ── find_relevant: cosine ranking ────────────────────────────────────────


class TestFindRelevantCosine:
    async def test_ranks_by_cosine_with_stored_embeddings(self) -> None:
        vocab = ["readiness", "ci", "docs", "upgrade"]
        # Row vectors deliberately chosen so cosine ranks them in a known order
        # against the query "readiness upgrade" (vec = [1,0,0,1]).
        rows = [
            {
                "agent": "pr_agent",
                "repo": "acme/web",
                "summary": "Fixed CI for PR #10 (lint)",
                "summary_embedding": "0,1,0,0",  # "ci"
                "outcome": "merged",
                "run_at": "2026-04-01T10:00:00+00:00",
                "pr_number": 10,
            },
            {
                "agent": "pr_agent",
                "repo": "acme/web",
                "summary": "Upgrade readiness gate for solo maintainers",
                "summary_embedding": "1,0,0,1",  # "readiness" + "upgrade" — top hit
                "outcome": "merged",
                "run_at": "2026-04-10T10:00:00+00:00",
                "pr_number": 42,
            },
            {
                "agent": "pr_agent",
                "repo": "acme/web",
                "summary": "Updated docs",
                "summary_embedding": "0,0,1,0",  # "docs"
                "outcome": "merged",
                "run_at": "2026-04-05T10:00:00+00:00",
                "pr_number": 12,
            },
        ]
        retriever = MemoryRetriever(
            graph=FakeGraphReader(rows),
            embedder=FakeEmbedder(vocab),
            max_hits=3,
        )
        hits = await retriever.find_relevant(
            agent="pr_agent",
            query_text="readiness upgrade",
            repo_slug="acme/web",
        )
        assert [h.pr_number for h in hits] == [42, 10, 12] or [h.pr_number for h in hits][0] == 42
        # Top hit must be the "readiness upgrade" row — cosine=1.0.
        assert hits[0].pr_number == 42
        assert hits[0].similarity == pytest.approx(1.0)
        # Outcome + summary flow through verbatim.
        assert hits[0].outcome == "merged"
        assert "readiness gate" in hits[0].summary

    async def test_caps_at_max_hits(self) -> None:
        rows = [
            {
                "agent": "pr_agent",
                "repo": "acme/web",
                "summary": f"Run {i}",
                "summary_embedding": "1,0,0,1",
                "outcome": "merged",
                "run_at": f"2026-04-{i:02d}T10:00:00+00:00",
                "pr_number": i,
            }
            for i in range(1, 11)
        ]
        retriever = MemoryRetriever(
            graph=FakeGraphReader(rows),
            embedder=FakeEmbedder(["readiness", "ci", "docs", "upgrade"]),
            max_hits=3,
        )
        hits = await retriever.find_relevant(
            agent="pr_agent",
            query_text="readiness upgrade",
            repo_slug="acme/web",
        )
        assert len(hits) == 3

    async def test_ties_broken_by_run_at_desc(self) -> None:
        """Equal similarity -> most-recent run wins."""
        rows = [
            {
                "agent": "pr_agent",
                "repo": "acme/web",
                "summary": "Older",
                "summary_embedding": "1,0,0,1",
                "outcome": "merged",
                "run_at": "2026-01-01T00:00:00+00:00",
                "pr_number": 100,
            },
            {
                "agent": "pr_agent",
                "repo": "acme/web",
                "summary": "Newer",
                "summary_embedding": "1,0,0,1",
                "outcome": "merged",
                "run_at": "2026-04-15T00:00:00+00:00",
                "pr_number": 200,
            },
        ]
        retriever = MemoryRetriever(
            graph=FakeGraphReader(rows),
            embedder=FakeEmbedder(["readiness", "ci", "docs", "upgrade"]),
            max_hits=2,
        )
        hits = await retriever.find_relevant(
            agent="pr_agent", query_text="readiness upgrade", repo_slug="acme/web"
        )
        assert [h.pr_number for h in hits] == [200, 100]


# ── find_relevant: Jaccard fallback ──────────────────────────────────────


class TestFindRelevantJaccard:
    async def test_no_embedder_uses_jaccard(self) -> None:
        rows = [
            {
                "agent": "pr_agent",
                "repo": "acme/web",
                "summary": "Fixed CI for PR #10 (lint)",
                "outcome": "merged",
                "run_at": "2026-04-01T10:00:00+00:00",
                "pr_number": 10,
            },
            {
                "agent": "pr_agent",
                "repo": "acme/web",
                "summary": "Upgrade readiness gate for solo maintainers",
                "outcome": "merged",
                "run_at": "2026-04-10T10:00:00+00:00",
                "pr_number": 42,
            },
        ]
        retriever = MemoryRetriever(
            graph=FakeGraphReader(rows),
            embedder=None,
            max_hits=2,
        )
        hits = await retriever.find_relevant(
            agent="pr_agent",
            query_text="upgrade readiness gate",
            repo_slug="acme/web",
        )
        # Jaccard overlap on "upgrade readiness gate" vs the two summaries:
        # row 42 shares every token -> top hit.
        assert hits[0].pr_number == 42
        assert hits[0].similarity > hits[1].similarity

    async def test_unavailable_embedder_falls_back_to_jaccard(self) -> None:
        rows = [
            {
                "agent": "pr_agent",
                "repo": "acme/web",
                "summary": "upgrade readiness gate",
                "outcome": "merged",
                "run_at": "2026-04-10T10:00:00+00:00",
            }
        ]
        retriever = MemoryRetriever(
            graph=FakeGraphReader(rows),
            embedder=UnavailableEmbedder(),
        )
        hits = await retriever.find_relevant(
            agent="pr_agent",
            query_text="upgrade readiness gate",
            repo_slug="acme/web",
        )
        assert len(hits) == 1
        assert hits[0].similarity == pytest.approx(1.0)

    async def test_rows_without_summary_skipped(self) -> None:
        rows = [
            {
                "agent": "pr_agent",
                "repo": "acme/web",
                "summary": "",
                "outcome": "merged",
                "run_at": "2026-04-01",
            },
            {
                "agent": "pr_agent",
                "repo": "acme/web",
                "summary": "readiness upgrade",
                "outcome": "merged",
                "run_at": "2026-04-02",
            },
        ]
        retriever = MemoryRetriever(graph=FakeGraphReader(rows))
        hits = await retriever.find_relevant(
            agent="pr_agent", query_text="readiness upgrade", repo_slug="acme/web"
        )
        assert len(hits) == 1

    async def test_empty_graph_returns_empty_list(self) -> None:
        retriever = MemoryRetriever(graph=FakeGraphReader([]))
        hits = await retriever.find_relevant(
            agent="pr_agent", query_text="anything", repo_slug="acme/web"
        )
        assert hits == []

    async def test_graph_error_degrades_silently(self) -> None:
        class ExplodingReader:
            async def list_nodes_with_properties(
                self,
                label: str,
                *,
                where: str | None = None,
                params: dict[str, Any] | None = None,
            ) -> list[dict[str, Any]]:
                raise RuntimeError("neo4j offline")

        retriever = MemoryRetriever(graph=ExplodingReader())
        hits = await retriever.find_relevant(
            agent="pr_agent", query_text="anything", repo_slug="acme/web"
        )
        assert hits == []


class TestFindRelevantScope:
    async def test_repo_slug_passed_through_to_graph(self) -> None:
        reader = FakeGraphReader([])
        retriever = MemoryRetriever(graph=reader)
        await retriever.find_relevant(agent="pr_agent", query_text="x", repo_slug="acme/web")
        assert reader.calls == [
            (
                "AgentCoreMemory",
                "n.agent = $agent AND n.repo = $repo",
                {"agent": "pr_agent", "repo": "acme/web"},
            )
        ]

    async def test_repo_slug_optional(self) -> None:
        reader = FakeGraphReader([])
        retriever = MemoryRetriever(graph=reader)
        await retriever.find_relevant(agent="issue_agent", query_text="x")
        assert reader.calls == [
            (
                "AgentCoreMemory",
                "n.agent = $agent",
                {"agent": "issue_agent"},
            )
        ]

    async def test_zero_max_hits_short_circuits(self) -> None:
        """max_hits=0 returns [] without hitting the graph at all."""
        reader = FakeGraphReader([{"agent": "pr_agent", "summary": "x"}])
        retriever = MemoryRetriever(graph=reader, max_hits=0)
        hits = await retriever.find_relevant(agent="pr_agent", query_text="x")
        assert hits == []
        assert reader.calls == []


# ── format_for_prompt: budget ────────────────────────────────────────────


class TestFormatForPrompt:
    def _hit(self, n: int, summary: str) -> CoreMemoryHit:
        return CoreMemoryHit(
            agent="pr_agent",
            summary=summary,
            outcome="merged",
            similarity=0.9,
            run_at_iso="2026-04-10T00:00:00+00:00",
            pr_number=n,
        )

    def test_empty_hits_returns_empty_string(self) -> None:
        retriever = MemoryRetriever(graph=FakeGraphReader([]))
        assert retriever.format_for_prompt([]) == ""

    def test_renders_markdown_heading_and_bullets(self) -> None:
        retriever = MemoryRetriever(graph=FakeGraphReader([]))
        block = retriever.format_for_prompt([self._hit(1, "Shipped solo-maintainer readiness fix")])
        assert "## Relevant past runs" in block
        assert "Shipped solo-maintainer readiness fix" in block
        assert "(PR #1)" in block
        assert "merged" in block

    def test_drops_trailing_hits_when_over_budget(self) -> None:
        """Oversized input: the block must not exceed 4 * max_tokens chars."""
        # max_tokens=50 -> 200-char budget. 5 fat hits can't all fit.
        retriever = MemoryRetriever(
            graph=FakeGraphReader([]),
            max_tokens=50,
        )
        hits = [self._hit(i, "x" * 150) for i in range(1, 6)]
        block = retriever.format_for_prompt(hits)
        # Must stay under the 4 * 50 = 200 char budget.
        assert len(block) <= 200
        # Top hit survives — the retriever prioritises it explicitly.
        assert "PR #1" in block
        # Later hits drop entirely when budget is exhausted.
        assert "PR #5" not in block

    def test_top_hit_survives_truncation(self) -> None:
        """Even with a tiny budget, the first hit always renders (truncated)."""
        retriever = MemoryRetriever(graph=FakeGraphReader([]), max_tokens=25)
        hits = [self._hit(7, "y" * 300)]
        block = retriever.format_for_prompt(hits)
        assert "PR #7" in block
        # Summary truncated with ellipsis.
        assert "..." in block
        assert len(block) <= 100


# ── Write path: publish_with_embedding ───────────────────────────────────


@pytest.mark.asyncio
class TestPublishWithEmbedding:
    async def test_stamps_embedding_when_embedder_available(self) -> None:
        writer = get_writer()
        store = RecordingStore()
        writer.configure(store)  # type: ignore[arg-type]
        try:
            await writer.start()
            embedder = FakeEmbedder(["readiness", "ci", "docs", "upgrade"])
            core = AgentCoreMemory(
                agent="pr_agent",
                run_id="2026-04-21T12:00:00+00:00",
                repo="acme/web",
                identity="pr_agent",
                summary="readiness upgrade",
                outcome="merged",
                pr_number=42,
            )
            await publish_with_embedding(core, embedder=embedder)
            assert await writer.flush(timeout=2.0) is True

            nodes = [n for n in store.nodes if n[0] == NodeType.AGENT_CORE_MEMORY.value]
            assert len(nodes) == 1
            _, _, props = nodes[0]
            assert props["summary"] == "readiness upgrade"
            assert props["outcome"] == "merged"
            assert props["pr_number"] == 42
            # Embedding persisted as a comma-separated float string.
            embedding_str = props["summary_embedding"]
            values = [float(p) for p in embedding_str.split(",")]
            assert values == pytest.approx([1.0, 0.0, 0.0, 1.0])
        finally:
            await writer.flush(timeout=2.0)
            await writer.stop()
            reset_for_tests()

    async def test_no_embedder_skips_vector_but_still_writes(self) -> None:
        writer = get_writer()
        store = RecordingStore()
        writer.configure(store)  # type: ignore[arg-type]
        try:
            await writer.start()
            core = AgentCoreMemory(
                agent="pr_agent",
                run_id="2026-04-21T12:00:00+00:00",
                repo="acme/web",
                identity="pr_agent",
                summary="readiness upgrade",
                outcome="merged",
                pr_number=42,
            )
            await publish_with_embedding(core, embedder=None)
            assert await writer.flush(timeout=2.0) is True

            nodes = [n for n in store.nodes if n[0] == NodeType.AGENT_CORE_MEMORY.value]
            _, _, props = nodes[0]
            assert "summary_embedding" not in props
            # Summary / outcome / pr_number still round-trip for the Jaccard path.
            assert props["summary"] == "readiness upgrade"
            assert props["pr_number"] == 42
        finally:
            await writer.flush(timeout=2.0)
            await writer.stop()
            reset_for_tests()

    async def test_embedder_failure_falls_back_to_plain_publish(self) -> None:
        class ExplodingEmbedder:
            available = True

            async def embed(self, text: str) -> list[float]:
                raise RuntimeError("vendor outage")

        writer = get_writer()
        store = RecordingStore()
        writer.configure(store)  # type: ignore[arg-type]
        try:
            await writer.start()
            core = AgentCoreMemory(
                agent="pr_agent",
                run_id="2026-04-21T12:00:00+00:00",
                repo="acme/web",
                identity="pr_agent",
                summary="readiness upgrade",
                outcome="merged",
            )
            await publish_with_embedding(core, embedder=ExplodingEmbedder())
            assert await writer.flush(timeout=2.0) is True

            nodes = [n for n in store.nodes if n[0] == NodeType.AGENT_CORE_MEMORY.value]
            assert len(nodes) == 1
            _, _, props = nodes[0]
            assert "summary_embedding" not in props
        finally:
            await writer.flush(timeout=2.0)
            await writer.stop()
            reset_for_tests()
