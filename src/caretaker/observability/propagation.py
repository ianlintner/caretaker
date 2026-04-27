"""W3C trace-context propagation across the Redis Streams event bus.

The webhook receiver and the consumer task that runs the dispatcher
live in different ``asyncio`` tasks (and often different pods). The
OTel context that the FastAPI instrumentor opens for ``POST /webhooks/github``
does **not** flow into the consumer task automatically — there's a
durable Redis hop in between, and contextvars don't cross processes.

We bridge it the same way HTTP propagators bridge a network call:
serialise the active context onto the message payload at publish time
(``traceparent`` + ``tracestate``), then re-hydrate it on the consumer
side. The OTel propagation API treats any ``MutableMapping[str, str]``
as a "carrier", so dropping the headers into the JSON payload dict
works without any custom encoding.

Producer side
-------------

    from caretaker.observability import inject_trace_context

    payload = webhook_event_payload(parsed)
    inject_trace_context(payload)  # adds traceparent/tracestate keys
    await bus.publish(stream, payload)

Consumer side
-------------

    from caretaker.observability import extracted_context

    parent_ctx = extracted_context(event.payload)
    tracer = trace.get_tracer("caretaker.eventbus")
    with tracer.start_as_current_span("consume", context=parent_ctx) as span:
        ...

Both helpers no-op cleanly when the OTel SDK is not installed, so call
sites stay branch-free.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


# ── Optional OTel imports ─────────────────────────────────────────────
#
# Mirrored from observability.otel: the ``otel`` extra is optional, so
# guard every symbol behind a try/except and degrade to no-op stubs
# when the SDK is missing.

_propagate: Any
_TRACE_CONTEXT_KEYS: tuple[str, ...] = ("traceparent", "tracestate")

try:  # pragma: no cover - exercised by the fallback path in tests
    from opentelemetry import propagate as _propagate  # type: ignore[no-redef, unused-ignore]

    _PROPAGATE_AVAILABLE = True
except ImportError:  # pragma: no cover
    _propagate = None
    _PROPAGATE_AVAILABLE = False


def inject_trace_context(payload: dict[str, Any]) -> None:
    """Stamp the active OTel context onto ``payload`` in-place.

    Adds ``traceparent`` and (when present) ``tracestate`` keys. Safe to
    call when no span is active — :func:`opentelemetry.propagate.inject`
    silently does nothing in that case. Never raises.
    """
    if not _PROPAGATE_AVAILABLE or _propagate is None:
        return
    try:
        _propagate.inject(payload)
    except Exception:  # pragma: no cover - defensive; inject is robust
        logger.debug("Failed to inject trace context into payload", exc_info=True)


def extracted_context(payload: dict[str, Any]) -> Any:
    """Return an OTel ``Context`` to use as ``context=`` parent for a span.

    Reads ``traceparent``/``tracestate`` keys off ``payload`` and feeds
    them through the global propagator. When the keys are absent (or
    OTel is not installed), returns whatever the propagator considers
    "no parent" — for the SDK that's a plain :class:`Context()`,
    yielding a fresh trace root.
    """
    if not _PROPAGATE_AVAILABLE or _propagate is None:
        return None
    try:
        # Filter to just the propagation keys so the propagator never
        # has to walk arbitrary payload entries.
        carrier = {k: payload[k] for k in _TRACE_CONTEXT_KEYS if k in payload}
        return _propagate.extract(carrier)
    except Exception:  # pragma: no cover
        logger.debug("Failed to extract trace context from payload", exc_info=True)
        return None


__all__ = ["extracted_context", "inject_trace_context"]
