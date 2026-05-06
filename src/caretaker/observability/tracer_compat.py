"""Optional-OTEL compatibility shim.

The ``opentelemetry`` packages live in the ``otel`` optional install
group. Caretaker code can still emit spans / set attributes / record
exceptions without the SDK installed — this module exposes the same
shape the SDK does, falling back to no-op stubs when the import fails.

Usage::

    from caretaker.observability.tracer_compat import get_tracer, Status, StatusCode
    _tracer = get_tracer("caretaker.pr_reviewer")

    with _tracer.start_as_current_span("op_name") as span:
        span.set_attribute("k", "v")
        span.record_exception(exc)
        span.set_status(Status(StatusCode.ERROR, "..."))

When OTEL is missing the calls become no-ops; behaviour is preserved.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any

try:
    from opentelemetry import trace as _otel_trace
    from opentelemetry.trace import Status, StatusCode

    OTEL_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only without otel extras
    _otel_trace = None  # type: ignore[assignment]
    OTEL_AVAILABLE = False

    class StatusCode:  # type: ignore[no-redef]
        OK = "ok"
        ERROR = "error"
        UNSET = "unset"

    class Status:  # type: ignore[no-redef]
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass


class _NullSpan:
    """Stub span that ignores every call. Returned by the null tracer."""

    def set_attribute(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def record_exception(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def set_status(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def get_span_context(self) -> None:
        return None

    def end(self, *_args: Any, **_kwargs: Any) -> None:
        pass


class _NullTracer:
    @contextmanager
    def start_as_current_span(self, _name: str, **_kwargs: Any):  # type: ignore[no-untyped-def]
        yield _NullSpan()


def get_tracer(name: str) -> Any:
    """Return a real OTEL tracer if installed, else a no-op stub."""
    if OTEL_AVAILABLE and _otel_trace is not None:
        return _otel_trace.get_tracer(name)
    return _NullTracer()


def get_current_span() -> Any:
    """Return the active span, or a null span when OTEL is unavailable."""
    if OTEL_AVAILABLE and _otel_trace is not None:
        return _otel_trace.get_current_span()
    return _NullSpan()


__all__ = [
    "OTEL_AVAILABLE",
    "Status",
    "StatusCode",
    "get_current_span",
    "get_tracer",
]
