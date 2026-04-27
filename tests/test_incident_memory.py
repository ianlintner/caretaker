"""Tests for :class:`IncidentMemory` publish path (Wave A3)."""

from __future__ import annotations

from typing import Any

import pytest

from caretaker.graph.models import NodeType, RelType
from caretaker.graph.writer import get_writer, reset_for_tests
from caretaker.memory.core import (
    IncidentMemory,
    publish_incident,
    publish_incident_with_embedding,
)


class _RecordingStore:
    def __init__(self) -> None:
        self.nodes: list[tuple[str, str, dict[str, Any]]] = []
        self.edges: list[tuple[str, str, str, str, str, dict[str, Any]]] = []

    async def merge_node(self, label: str, node_id: str, props: dict[str, Any]) -> None:
        self.nodes.append((label, node_id, dict(props)))

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
            (source_label, source_id, target_label, target_id, rel_type, dict(properties or {}))
        )


class _FakeEmbedder:
    def __init__(self, *, available: bool = True, vector: list[float] | None = None) -> None:
        self._available = available
        self._vector = vector if vector is not None else [0.7, 0.8, 0.9]

    @property
    def available(self) -> bool:
        return self._available

    async def embed(self, _text: str) -> list[float]:
        return list(self._vector)


@pytest.fixture
async def recording_writer():  # type: ignore[no-untyped-def]
    reset_for_tests()
    writer = get_writer()
    store = _RecordingStore()
    writer.configure(store)  # type: ignore[arg-type]
    await writer.start()
    try:
        yield store, writer
    finally:
        await writer.stop()
        reset_for_tests()


class TestIncidentPublish:
    async def test_publish_emits_node_and_edge(self, recording_writer) -> None:  # type: ignore[no-untyped-def]
        store, writer = recording_writer
        incident = IncidentMemory(
            repo="o/r",
            error_signature="abc123",
            kind="lint_failure",
            job_name="lint",
            summary="Lint failure fixed by ruff-format",
            fix_outcome="fixed",
            run_id="run-1",
            rungs_tried=["ruff-format"],
        )
        publish_incident(incident)
        assert await writer.flush(timeout=2.0)
        # Node + edge were written
        assert len(store.nodes) == 1
        label, node_id, props = store.nodes[0]
        assert label == NodeType.INCIDENT.value
        assert node_id == "incident:o/r:abc123"
        assert props["fix_outcome"] == "fixed"
        assert props["rungs_tried"] == "ruff-format"
        # No embedding stamped when not supplied
        assert "summary_embedding" not in props

        assert len(store.edges) == 1
        src_label, src_id, tgt_label, tgt_id, rel, _edge_props = store.edges[0]
        assert src_label == NodeType.INCIDENT.value
        assert tgt_label == NodeType.REPO.value
        assert tgt_id == "repo:o/r"
        assert rel == RelType.REFERENCES.value

    async def test_publish_with_embedding_stamps_vector_when_enabled(
        self, recording_writer
    ) -> None:  # type: ignore[no-untyped-def]
        store, writer = recording_writer
        incident = IncidentMemory(
            repo="o/r",
            error_signature="def456",
            kind="type",
            job_name="mypy",
            summary="Type error in foo.py",
            fix_outcome="escalated",
        )
        await publish_incident_with_embedding(
            incident,
            embedder=_FakeEmbedder(),
            write_embeddings=True,
        )
        assert await writer.flush(timeout=2.0)
        _label, _node_id, props = store.nodes[0]
        assert "summary_embedding" in props
        # Comma-joined float string — three floats
        assert props["summary_embedding"].count(",") == 2

    async def test_publish_with_embedding_skips_vector_when_disabled(
        self, recording_writer
    ) -> None:  # type: ignore[no-untyped-def]
        store, writer = recording_writer
        incident = IncidentMemory(
            repo="o/r",
            error_signature="def456",
            kind="type",
            job_name="mypy",
            summary="Type error in foo.py",
            fix_outcome="escalated",
        )
        await publish_incident_with_embedding(
            incident,
            embedder=_FakeEmbedder(),
            write_embeddings=False,  # explicitly off
        )
        assert await writer.flush(timeout=2.0)
        _label, _node_id, props = store.nodes[0]
        # Write-path embedding toggle is off → no embedding stamped
        assert "summary_embedding" not in props

    async def test_publish_with_embedding_no_embedder(self, recording_writer) -> None:  # type: ignore[no-untyped-def]
        store, writer = recording_writer
        incident = IncidentMemory(
            repo="o/r",
            error_signature="ghi789",
            kind="lint",
            job_name="lint",
            summary="Lint fail",
            fix_outcome="escalated",
        )
        await publish_incident_with_embedding(
            incident,
            embedder=None,
            write_embeddings=True,
        )
        assert await writer.flush(timeout=2.0)
        _label, _node_id, props = store.nodes[0]
        assert "summary_embedding" not in props
