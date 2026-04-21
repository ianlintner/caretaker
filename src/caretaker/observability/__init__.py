"""Observability helpers — OpenTelemetry GenAI agent spans (M8).

Caretaker's default install deliberately does **not** pull in the OTel
SDK. Operators opt in by installing the ``otel`` extra and pointing
``OTEL_EXPORTER_OTLP_ENDPOINT`` at any GenAI-aware backend (Phoenix,
Datadog, LangSmith). Every public helper in :mod:`caretaker.observability.otel`
is a no-op when the SDK is missing or the endpoint is unset, so call
sites never branch on availability.
"""

from caretaker.observability.otel import (
    agent_span,
    current_span_ids,
    init_tracing,
)

__all__ = [
    "agent_span",
    "current_span_ids",
    "init_tracing",
]
