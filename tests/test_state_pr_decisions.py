"""Tests for the per-PR decision-timeline writer."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import pytest

from caretaker.state.pr_decisions import (
    PRDecisionStore,
    record_decision,
)
from caretaker.state.pr_decisions import (
    configure_default_store as _configure_default_store,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

# Skip the trace_id test if the OTel SDK isn't installed.
_SDK_OK = True
try:  # pragma: no cover - import guard
    import opentelemetry.sdk.trace  # noqa: F401
except Exception:  # pragma: no cover - SDK absent
    _SDK_OK = False


@pytest.fixture(autouse=True)
def _reset_default_store() -> Iterator[None]:
    """Make sure cross-test pollution of the module-level singleton can't bite."""
    _configure_default_store(None)
    yield
    _configure_default_store(None)


class _FakeCursor:
    """Minimal AsyncIterator over an in-memory list, supporting sort/skip/limit chains."""

    def __init__(self, docs: list[dict[str, Any]]) -> None:
        self._docs = docs

    def sort(self, key: str, direction: int) -> _FakeCursor:
        reverse = direction < 0
        self._docs = sorted(self._docs, key=lambda d: d[key], reverse=reverse)
        return self

    def skip(self, n: int) -> _FakeCursor:
        self._docs = self._docs[int(n) :]
        return self

    def limit(self, n: int) -> _FakeCursor:
        self._docs = self._docs[: int(n)]
        return self

    def __aiter__(self) -> _FakeCursor:
        self._iter = iter(self._docs)
        return self

    async def __anext__(self) -> dict[str, Any]:
        try:
            return next(self._iter)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


class _FakeCollection:
    """Stand-in for an ``AsyncIOMotorCollection``."""

    def __init__(self) -> None:
        self.inserts: list[dict[str, Any]] = []
        self.create_index = AsyncMock(return_value="idx")
        self._fail_insert = False
        self._docs: list[dict[str, Any]] = []

    async def insert_one(self, doc: dict[str, Any]) -> Any:
        if self._fail_insert:
            raise RuntimeError("simulated mongo down")
        self.inserts.append(doc)
        self._docs.append(doc)
        return None

    def find(self, query: dict[str, Any]) -> _FakeCursor:
        matched = [d for d in self._docs if all(d.get(k) == v for k, v in query.items())]
        return _FakeCursor(matched)

    async def count_documents(self, query: dict[str, Any]) -> int:
        return sum(1 for d in self._docs if all(d.get(k) == v for k, v in query.items()))


def _wire_fake_collection(store: PRDecisionStore, col: _FakeCollection) -> None:
    """Stub out ``_ensure_collection`` so tests don't touch a real Mongo."""

    async def _stub() -> _FakeCollection:
        return col

    store._ensure_collection = _stub  # type: ignore[assignment]


class TestPRDecisionStore:
    @pytest.mark.asyncio
    async def test_record_decision_writes_document(self) -> None:
        store = PRDecisionStore(enabled=True)
        col = _FakeCollection()
        _wire_fake_collection(store, col)
        _configure_default_store(store)

        await record_decision(
            "ianlintner/caretaker-qa",
            108,
            "pr_reviewer",
            "review_started",
            author="ianlintner",
            is_caretaker_owned=False,
        )

        assert len(col.inserts) == 1
        doc = col.inserts[0]
        assert doc["repo"] == "ianlintner/caretaker-qa"
        assert doc["pr_number"] == 108
        assert doc["agent"] == "pr_reviewer"
        assert doc["event"] == "review_started"
        assert doc["fields"] == {"author": "ianlintner", "is_caretaker_owned": False}
        assert isinstance(doc["observed_at"], datetime)
        assert isinstance(doc["id"], str) and len(doc["id"]) > 0

    @pytest.mark.asyncio
    async def test_record_decision_when_mongo_down(self) -> None:
        store = PRDecisionStore(enabled=True)
        col = _FakeCollection()
        col._fail_insert = True
        _wire_fake_collection(store, col)
        _configure_default_store(store)

        # MUST NOT raise — observability never fails the agent run.
        await record_decision(
            "ianlintner/caretaker-qa",
            108,
            "pr_reviewer",
            "review_started",
            author="x",
        )

        # _client should be cleared so the next call retries the connect path.
        assert store._client is None

    @pytest.mark.skipif(not _SDK_OK, reason="OTel SDK not installed")
    @pytest.mark.asyncio
    async def test_record_decision_captures_active_trace_id(self) -> None:
        from opentelemetry import trace as otel_trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
            InMemorySpanExporter,
        )

        exporter = InMemorySpanExporter()
        current = otel_trace.get_tracer_provider()
        if hasattr(current, "add_span_processor"):
            current.add_span_processor(SimpleSpanProcessor(exporter))
        else:
            provider = TracerProvider(resource=Resource.create({"service.name": "test"}))
            provider.add_span_processor(SimpleSpanProcessor(exporter))
            otel_trace.set_tracer_provider(provider)

        store = PRDecisionStore(enabled=True)
        col = _FakeCollection()
        _wire_fake_collection(store, col)
        _configure_default_store(store)

        tracer = otel_trace.get_tracer("test")
        with tracer.start_as_current_span("test-span"):
            await record_decision(
                "ianlintner/caretaker-qa",
                108,
                "pr_reviewer",
                "review_started",
            )

        assert len(col.inserts) == 1
        doc = col.inserts[0]
        # Hex-encoded trace + span ids — non-empty when an active span exists.
        assert isinstance(doc["trace_id"], str) and len(doc["trace_id"]) == 32
        assert isinstance(doc["span_id"], str) and len(doc["span_id"]) == 16

    @pytest.mark.asyncio
    async def test_query_timeline_returns_sorted_documents(self) -> None:
        store = PRDecisionStore(enabled=True)
        col = _FakeCollection()
        _wire_fake_collection(store, col)

        now = datetime.now(UTC)
        # Insert out-of-order: middle, oldest, newest.
        for offset_minutes, event in (
            (5, "tier_classified"),
            (10, "review_started"),
            (1, "review_completed"),
        ):
            col._docs.append(
                {
                    "id": f"id-{event}",
                    "repo": "ianlintner/caretaker-qa",
                    "pr_number": 108,
                    "observed_at": now - timedelta(minutes=offset_minutes),
                    "agent": "pr_reviewer",
                    "event": event,
                    "fields": {},
                    "trace_id": None,
                    "span_id": None,
                }
            )

        timeline, total_count = await store.query_timeline(
            owner="ianlintner", repo="caretaker-qa", pr_number=108
        )

        assert [d["event"] for d in timeline] == [
            "review_started",
            "tier_classified",
            "review_completed",
        ]
        assert total_count == 3

    @pytest.mark.asyncio
    async def test_query_timeline_pagination_reports_total(self) -> None:
        """``query_timeline`` honours ``limit``/``offset`` and reports total_count."""
        store = PRDecisionStore(enabled=True)
        col = _FakeCollection()
        _wire_fake_collection(store, col)

        now = datetime.now(UTC)
        # Insert 5 decisions, oldest first.
        for i in range(5):
            col._docs.append(
                {
                    "id": f"id-{i}",
                    "repo": "ianlintner/caretaker-qa",
                    "pr_number": 108,
                    "observed_at": now - timedelta(minutes=10 - i),
                    "agent": "pr_reviewer",
                    "event": f"event_{i}",
                    "fields": {},
                    "trace_id": None,
                    "span_id": None,
                }
            )

        # First page: 2 items, total_count = 5.
        page1, total1 = await store.query_timeline(
            owner="ianlintner", repo="caretaker-qa", pr_number=108, limit=2, offset=0
        )
        assert [d["id"] for d in page1] == ["id-0", "id-1"]
        assert total1 == 5

        # Second page: 2 items at offset 2, total_count still 5.
        page2, total2 = await store.query_timeline(
            owner="ianlintner", repo="caretaker-qa", pr_number=108, limit=2, offset=2
        )
        assert [d["id"] for d in page2] == ["id-2", "id-3"]
        assert total2 == 5
