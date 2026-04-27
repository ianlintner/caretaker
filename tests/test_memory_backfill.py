"""Tests for the memory embedding backfill CLI (Wave A3)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from caretaker.memory.backfill import _parse_since, run_backfill_async


class _FakeEmbedder:
    def __init__(
        self,
        *,
        available: bool = True,
        vector: list[float] | None = None,
        raises: Exception | None = None,
    ) -> None:
        self._available = available
        self._vector = vector if vector is not None else [0.1, 0.2, 0.3]
        self._raises = raises
        self.calls: list[str] = []

    @property
    def available(self) -> bool:
        return self._available

    async def embed(self, text: str) -> list[float]:
        self.calls.append(text)
        if self._raises is not None:
            raise self._raises
        return list(self._vector)


class _FakeStore:
    def __init__(self, rows_by_label: dict[str, list[dict[str, Any]]]) -> None:
        self._rows_by_label = rows_by_label
        self.merged: list[tuple[str, str, dict[str, Any]]] = []

    async def list_nodes_with_properties(
        self,
        label: str,
        *,
        where: str | None = None,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        return list(self._rows_by_label.get(label, []))

    async def merge_node(self, label: str, node_id: str, props: dict[str, Any]) -> None:
        self.merged.append((label, node_id, dict(props)))


class TestParseSince:
    def test_days(self) -> None:
        assert _parse_since("30d") == timedelta(days=30)

    def test_hours(self) -> None:
        assert _parse_since("72h") == timedelta(hours=72)

    def test_weeks(self) -> None:
        assert _parse_since("2w") == timedelta(weeks=2)

    def test_invalid(self) -> None:
        with pytest.raises(ValueError):
            _parse_since("forever")


class TestBackfill:
    async def test_backfill_populates_nodes_without_embedding(self) -> None:
        recent = datetime.now(UTC).isoformat()
        rows = {
            "Incident": [
                {
                    "id": "incident:o/r:abc",
                    "summary": "Self-heal escalated in lint job",
                    "observed_at": recent,
                },
                {
                    "id": "incident:o/r:def",
                    "summary": "Self-heal fixed by ruff-format",
                    "observed_at": recent,
                    "summary_embedding": "0.1,0.2",  # already has one → skip
                },
            ],
            "AgentCoreMemory": [
                {
                    "id": "acm:self_heal:run-1",
                    "summary": "Agent core memory with a summary",
                    "observed_at": recent,
                },
                {
                    "id": "acm:self_heal:run-2",
                    # no summary → skipped
                    "observed_at": recent,
                },
            ],
        }
        embedder = _FakeEmbedder()
        store = _FakeStore(rows)

        summary = await run_backfill_async(
            store=store,  # type: ignore[arg-type]
            embedder=embedder,  # type: ignore[arg-type]
            since="30d",
            labels=["Incident", "AgentCoreMemory"],
        )

        assert summary["updated"] == 2  # one Incident + one AgentCoreMemory
        assert summary["skipped_has_embedding"] == 1
        assert summary["skipped_no_summary"] == 1
        assert len(store.merged) == 2
        for label, node_id, props in store.merged:
            assert label in {"Incident", "AgentCoreMemory"}
            assert node_id.startswith(("incident:", "acm:"))
            assert "summary_embedding" in props
            assert "," in props["summary_embedding"]  # comma-joined

    async def test_backfill_dry_run_counts_without_writing(self) -> None:
        recent = datetime.now(UTC).isoformat()
        rows = {
            "Incident": [
                {
                    "id": "incident:o/r:abc",
                    "summary": "x",
                    "observed_at": recent,
                },
            ],
            "AgentCoreMemory": [],
        }
        embedder = _FakeEmbedder()
        store = _FakeStore(rows)
        summary = await run_backfill_async(
            store=store,  # type: ignore[arg-type]
            embedder=embedder,  # type: ignore[arg-type]
            labels=["Incident", "AgentCoreMemory"],
            dry_run=True,
        )
        assert summary["updated"] == 1
        assert store.merged == []  # no writes in dry_run

    async def test_backfill_reports_no_writes_when_embedder_missing(self) -> None:
        recent = datetime.now(UTC).isoformat()
        rows = {
            "Incident": [
                {
                    "id": "incident:o/r:abc",
                    "summary": "x",
                    "observed_at": recent,
                },
            ],
            "AgentCoreMemory": [],
        }
        store = _FakeStore(rows)
        summary = await run_backfill_async(
            store=store,  # type: ignore[arg-type]
            embedder=None,
            labels=["Incident", "AgentCoreMemory"],
        )
        assert summary["updated"] == 0
        assert summary["skipped_no_embedder"] == 1
        assert store.merged == []

    async def test_backfill_surfaces_embed_errors(self) -> None:
        recent = datetime.now(UTC).isoformat()
        rows = {
            "Incident": [
                {
                    "id": "incident:o/r:abc",
                    "summary": "x",
                    "observed_at": recent,
                },
            ],
            "AgentCoreMemory": [],
        }
        embedder = _FakeEmbedder(raises=RuntimeError("boom"))
        store = _FakeStore(rows)
        summary = await run_backfill_async(
            store=store,  # type: ignore[arg-type]
            embedder=embedder,  # type: ignore[arg-type]
            labels=["Incident"],
        )
        assert summary["updated"] == 0
        assert any("embed failed" in e for e in summary["errors"])
