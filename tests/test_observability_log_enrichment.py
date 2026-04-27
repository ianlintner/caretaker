"""``RunStreamHandler`` log enrichment with OTel trace_id / span_id.

When a span is active, every ``LogEntry`` appended to a run's stream
must carry ``trace_id`` and ``span_id`` tags so the admin SSE viewer
can render a clickable Tempo link. Outside a span (CLI startup,
non-traced contexts), the tags must be omitted so the run stream
isn't bloated with empty correlation ids.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from caretaker.runs.models import LogEntry

pytest.importorskip("opentelemetry.sdk.trace")

from opentelemetry import trace as otel_trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from caretaker.observability.bootstrap import _install_otel_log_record_factory
from caretaker.runs.dispatch import (
    RunStreamHandler,
    _current_run_id,
    _current_seq,
)


class _FakeStore:
    """Captures every ``append_log`` call so the test can assert on tags."""

    def __init__(self) -> None:
        self.entries: list[tuple[str, LogEntry]] = []

    async def append_log(self, run_id: str, entry: LogEntry) -> None:
        self.entries.append((run_id, entry))

    # Other RunsStore methods are not exercised by RunStreamHandler.
    def __getattr__(self, name: str) -> Any:  # pragma: no cover
        raise AttributeError(name)


_TRACER_PROVIDER_SETUP = False


@pytest.fixture(autouse=True)
def _otel_provider() -> InMemorySpanExporter:
    """Module-level TracerProvider + caretaker's LogRecord factory.

    OTel only lets ``set_tracer_provider`` succeed once per process,
    so we set it on the first fixture invocation and reuse it. Each
    test swaps in a fresh exporter via ``add_span_processor``.

    We install caretaker's own ``otelTraceID``/``otelSpanID`` LogRecord
    factory (the same one ``bootstrap_observability`` installs in
    production) instead of the contrib LoggingInstrumentor — the
    contrib variant only stamps attributes when it also rewrites the
    log format string, which we deliberately don't want.
    """
    global _TRACER_PROVIDER_SETUP
    exporter = InMemorySpanExporter()
    if not _TRACER_PROVIDER_SETUP:
        provider = TracerProvider(resource=Resource.create({"service.name": "test"}))
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        otel_trace.set_tracer_provider(provider)
        _install_otel_log_record_factory()
        _TRACER_PROVIDER_SETUP = True
    else:
        provider = otel_trace.get_tracer_provider()
        if hasattr(provider, "add_span_processor"):
            provider.add_span_processor(SimpleSpanProcessor(exporter))
    yield exporter
    exporter.clear()


@pytest.fixture
def run_context() -> tuple[str, list[int]]:
    run_id = "run-test-1"
    seq_holder = [0]
    rt = _current_run_id.set(run_id)
    st = _current_seq.set(seq_holder)
    yield run_id, seq_holder
    _current_run_id.reset(rt)
    _current_seq.reset(st)


def _emit_one(handler: RunStreamHandler, message: str = "hello") -> None:
    record = logging.LogRecord(
        name="caretaker.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=None,
    )
    handler.emit(record)


async def _drain() -> None:
    """Wait one event loop tick so fire-and-forget append_log tasks finish."""
    await asyncio.sleep(0)
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_log_entry_carries_trace_id_when_span_active(
    run_context: tuple[str, list[int]],
) -> None:
    store = _FakeStore()
    handler = RunStreamHandler(store)

    tracer = otel_trace.get_tracer("test")
    # ``LoggingInstrumentor`` sets ``otelTraceID``/``otelSpanID`` via a
    # logging.Filter that runs *before* handler.emit. We need a real
    # logger.handle path so the filter runs — emitting via handler.emit
    # directly bypasses the standard pre-handle pipeline. Reproduce
    # what the production path does: log via a logger.
    log = logging.getLogger("caretaker.test_log_enrichment")
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    try:
        with tracer.start_as_current_span("test-span") as span:
            expected_trace = format(span.get_span_context().trace_id, "032x")
            expected_span = format(span.get_span_context().span_id, "016x")
            log.info("inside span")
            await _drain()
    finally:
        log.removeHandler(handler)

    assert len(store.entries) == 1
    _, entry = store.entries[0]
    assert entry.tags.get("trace_id") == expected_trace
    assert entry.tags.get("span_id") == expected_span
    assert entry.tags.get("logger") == "caretaker.test_log_enrichment"


@pytest.mark.asyncio
async def test_log_entry_omits_trace_tags_outside_span(
    run_context: tuple[str, list[int]],
) -> None:
    store = _FakeStore()
    handler = RunStreamHandler(store)
    log = logging.getLogger("caretaker.test_log_enrichment.outside")
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    try:
        log.info("no span")
        await _drain()
    finally:
        log.removeHandler(handler)

    assert len(store.entries) == 1
    _, entry = store.entries[0]
    # No trace ⇒ tags should not include the all-zero correlation ids.
    assert "trace_id" not in entry.tags
    assert "span_id" not in entry.tags


@pytest.mark.asyncio
async def test_handler_skips_when_no_run_id() -> None:
    """Outside a run context the handler must not append anything."""
    store = _FakeStore()
    handler = RunStreamHandler(store)
    log = logging.getLogger("caretaker.test_log_enrichment.norun")
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    try:
        log.info("orphan")
        await _drain()
    finally:
        log.removeHandler(handler)

    assert store.entries == []
