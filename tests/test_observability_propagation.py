"""End-to-end OTel trace context propagation across the event-bus hop.

The webhook receiver and the dispatcher consumer live in different
asyncio tasks (and often different pods). The OTel context that
``POST /webhooks/github`` opens does **not** flow into the consumer
automatically — there's a Redis hop in between.

These tests verify the bridge:

1. ``inject_trace_context`` writes a W3C ``traceparent`` onto the
   payload when called inside an active span.
2. ``extracted_context`` returns a context whose trace_id matches what
   was injected.
3. The payload builders in ``eventbus.consumer`` invoke the injection
   automatically, so call sites don't have to remember.
4. The full publish → consume cycle preserves the trace_id.
"""

from __future__ import annotations

import importlib

import pytest

# Skip the entire module when the OTel SDK isn't installed — these
# tests exercise the propagator wiring, not the no-op fallback.
pytest.importorskip("opentelemetry.sdk.trace")
pytest.importorskip("opentelemetry.exporter.otlp.proto.grpc.trace_exporter")

from opentelemetry import trace as otel_trace
from opentelemetry.propagate import set_global_textmap
from opentelemetry.propagators.composite import CompositePropagator
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from caretaker.eventbus.consumer import webhook_event_payload
from caretaker.github_app.webhooks import ParsedWebhook
from caretaker.observability import (
    extracted_context,
    inject_trace_context,
)


@pytest.fixture
def in_memory_tracer() -> InMemorySpanExporter:
    """Install a tracer provider that captures spans in-process.

    Re-installable: each test gets a fresh exporter so assertions on
    ``get_finished_spans()`` don't see leftovers from earlier tests.
    """
    exporter = InMemorySpanExporter()
    provider = TracerProvider(resource=Resource.create({"service.name": "test"}))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    otel_trace.set_tracer_provider(provider)
    set_global_textmap(CompositePropagator([TraceContextTextMapPropagator()]))
    yield exporter
    exporter.clear()


def _parsed() -> ParsedWebhook:
    return ParsedWebhook(
        event_type="pull_request",
        delivery_id="abc-123",
        action="opened",
        installation_id=42,
        repository_full_name="owner/repo",
        payload={"action": "opened", "pull_request": {"number": 7}},
    )


class TestInjectExtract:
    def test_inject_no_active_span_leaves_payload_clean(
        self, in_memory_tracer: InMemorySpanExporter
    ) -> None:
        """Without an active span, inject is a no-op (no traceparent key)."""
        payload: dict[str, object] = {"kind": "webhook"}
        inject_trace_context(payload)
        assert "traceparent" not in payload

    def test_inject_inside_span_adds_traceparent(
        self, in_memory_tracer: InMemorySpanExporter
    ) -> None:
        tracer = otel_trace.get_tracer("test")
        with tracer.start_as_current_span("publisher"):
            payload: dict[str, object] = {"kind": "webhook"}
            inject_trace_context(payload)
            assert "traceparent" in payload
            tp = str(payload["traceparent"])
            # W3C v0: 00-<32hex>-<16hex>-<flags>
            assert tp.startswith("00-")
            assert len(tp.split("-")) == 4

    def test_extract_round_trip_preserves_trace_id(
        self, in_memory_tracer: InMemorySpanExporter
    ) -> None:
        tracer = otel_trace.get_tracer("test")
        with tracer.start_as_current_span("producer") as producer_span:
            producer_trace_id = format(producer_span.get_span_context().trace_id, "032x")
            payload: dict[str, object] = {"kind": "webhook"}
            inject_trace_context(payload)

        # Outside the producer span context now — start a fresh "consumer"
        # span using the extracted parent and verify the trace_id matches.
        parent_ctx = extracted_context(payload)
        with tracer.start_as_current_span("consumer", context=parent_ctx) as consumer_span:
            consumer_trace_id = format(consumer_span.get_span_context().trace_id, "032x")

        assert consumer_trace_id == producer_trace_id


class TestPayloadBuildersInjectAutomatically:
    """The eventbus payload builders must inject trace context for free."""

    def test_webhook_event_payload_carries_traceparent(
        self, in_memory_tracer: InMemorySpanExporter
    ) -> None:
        # Force-reload consumer so it picks up the freshly installed
        # propagator (importing the module before the fixture ran would
        # have cached a propagator-less reference).
        from caretaker.eventbus import consumer as consumer_module

        importlib.reload(consumer_module)

        tracer = otel_trace.get_tracer("test")
        with tracer.start_as_current_span("POST /webhooks/github") as root:
            expected_trace_id = format(root.get_span_context().trace_id, "032x")
            payload = consumer_module.webhook_event_payload(_parsed())

        assert "traceparent" in payload
        assert expected_trace_id in str(payload["traceparent"])

    def test_run_trigger_event_payload_carries_traceparent(
        self, in_memory_tracer: InMemorySpanExporter
    ) -> None:
        from caretaker.eventbus import consumer as consumer_module

        importlib.reload(consumer_module)

        tracer = otel_trace.get_tracer("test")
        with tracer.start_as_current_span("POST /runs/{id}/trigger") as root:
            expected_trace_id = format(root.get_span_context().trace_id, "032x")
            payload = consumer_module.run_trigger_event_payload(
                parsed=_parsed(), run_id="run-1", last_seq=0
            )

        assert "traceparent" in payload
        assert expected_trace_id in str(payload["traceparent"])


class TestEndToEndTraceContinuity:
    def test_publish_then_consume_share_trace_id(
        self, in_memory_tracer: InMemorySpanExporter
    ) -> None:
        """The whole point: producer trace_id == consumer trace_id."""
        tracer = otel_trace.get_tracer("test")

        # Publisher side
        with tracer.start_as_current_span("publisher") as publisher_span:
            producer_trace_id = format(publisher_span.get_span_context().trace_id, "032x")
            payload = webhook_event_payload(_parsed())

        # Consumer side — different async task in real life
        parent_ctx = extracted_context(payload)
        with tracer.start_as_current_span(
            "eventbus.consume webhook", context=parent_ctx
        ) as consume_span:
            consumer_trace_id = format(consume_span.get_span_context().trace_id, "032x")

        assert producer_trace_id == consumer_trace_id


class TestSafetyContract:
    """Helpers must never raise. Production code calls them in hot paths."""

    def test_inject_with_none_payload_does_not_raise(self) -> None:
        # Defensive: callers should always pass a dict, but if they
        # mistakenly pass ``None`` (or some other type), we still don't
        # take down the dispatch path.
        try:
            inject_trace_context({})  # type: ignore[arg-type]
        except Exception as exc:  # pragma: no cover
            pytest.fail(f"inject_trace_context raised: {exc!r}")

    def test_extract_missing_keys_returns_usable_context(self) -> None:
        # Empty payload → fresh root context (no parent), still valid.
        ctx = extracted_context({})
        # Either ``None`` (when OTel absent) or a Context object (when
        # present). Both must be acceptable as ``context=`` arg.
        tracer = otel_trace.get_tracer("test")
        with tracer.start_as_current_span("orphan", context=ctx):
            pass
