"""Tests for the OTEL compatibility shim.

The shim must:
* expose a ``get_tracer`` callable returning something with the right
  ``start_as_current_span`` shape, regardless of whether the
  ``opentelemetry`` SDK is installed;
* yield a span object that swallows every call without raising;
* expose ``Status`` / ``StatusCode`` symbols that can be instantiated
  even when the real SDK is absent.

The tests below exercise the no-op classes directly so they pass
both with and without the otel extras installed.
"""

from __future__ import annotations

from caretaker.observability.tracer_compat import (
    Status,
    StatusCode,
    _NullSpan,
    _NullTracer,
    get_current_span,
    get_tracer,
)


def test_get_tracer_returns_object_with_start_as_current_span() -> None:
    """``get_tracer`` always returns an object with the SDK-shaped API."""
    tracer = get_tracer("caretaker.test")
    assert hasattr(tracer, "start_as_current_span")


def test_null_span_swallows_all_calls() -> None:
    """The no-op span must accept every SDK-shaped call without raising."""
    span = _NullSpan()
    # All four span methods used elsewhere in the codebase must be no-ops.
    span.set_attribute("k", "v")
    span.record_exception(RuntimeError("boom"))
    span.set_status(Status(StatusCode.ERROR, "msg"))
    # ``get_span_context`` always returns ``None`` on the null span so
    # ``_capture_trace_ids`` short-circuits cleanly.
    span.get_span_context()
    span.end()


def test_null_tracer_context_manager_yields_null_span() -> None:
    """``with tracer.start_as_current_span(...)`` must yield a null-shaped span."""
    tracer = _NullTracer()
    with tracer.start_as_current_span("op") as span:
        # The yielded span must support every call we make at usage sites.
        span.set_attribute("caretaker.k", "v")
        span.record_exception(ValueError("x"))
        span.set_status(Status(StatusCode.ERROR, "y"))


def test_status_and_statuscode_constructable_without_sdk() -> None:
    """Status / StatusCode must always be importable + constructable."""
    # The real SDK signature is ``Status(StatusCode.ERROR, description)``;
    # the fallback accepts the same args and ignores them. Either way
    # the constructor must not raise.
    assert Status(StatusCode.ERROR, "msg") is not None
    # All three semantic codes must exist as attributes.
    assert StatusCode.OK is not None
    assert StatusCode.ERROR is not None
    assert StatusCode.UNSET is not None


def test_get_current_span_returns_object_with_get_span_context() -> None:
    """``get_current_span`` must always return something with ``get_span_context``."""
    span = get_current_span()
    assert hasattr(span, "get_span_context")
