"""Observability helpers — OpenTelemetry GenAI agent spans (M8).

Caretaker's default install deliberately does **not** pull in the OTel
SDK. Operators opt in by installing the ``otel`` extra and pointing
``OTEL_EXPORTER_OTLP_ENDPOINT`` at any GenAI-aware backend (Phoenix,
Datadog, LangSmith). Every public helper in :mod:`caretaker.observability.otel`
is a no-op when the SDK is missing or the endpoint is unset, so call
sites never branch on availability.
"""

from caretaker.observability.metrics import (
    init_metrics,
    record_error,
    record_http_client,
    record_worker_job,
    set_rate_limit_cooldown,
    set_rate_limit_remaining,
    set_worker_queue_depth,
    start_metrics_server,
    stop_metrics_server,
    timed_op,
)
from caretaker.observability.otel import (
    agent_span,
    current_span_ids,
    init_tracing,
)

__all__ = [
    "agent_span",
    "current_span_ids",
    "init_metrics",
    "init_tracing",
    "record_error",
    "record_http_client",
    "record_worker_job",
    "set_rate_limit_cooldown",
    "set_rate_limit_remaining",
    "set_worker_queue_depth",
    "start_metrics_server",
    "stop_metrics_server",
    "timed_op",
]
