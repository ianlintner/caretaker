"""OpenTelemetry tracing — agent GenAI spans + cluster-wide e2e wiring.

Caretaker adopts the April 2026 OpenTelemetry GenAI semantic
conventions — every agent run emits a single ``invoke_agent`` span
with ``gen_ai.agent.name`` + ``gen_ai.operation.name`` attributes.
The span id is mirrored into :class:`~caretaker.causal_chain.CausalEvent`
rows so "which span caused this escalation" is a one-hop query against
either the graph or the trace backend.

This module owns the SDK setup. The full e2e wiring
(auto-instrumentors, log enrichment, cross-process propagation) lives
in sibling modules:

* :mod:`caretaker.observability.bootstrap` — single entry-point helper
  every long-running caretaker process calls at startup.
* :mod:`caretaker.observability.propagation` — W3C trace-context
  inject/extract for the Redis Streams event-bus hop.
* :mod:`caretaker.observability.llm_span` — manual GenAI spans around
  LLM calls (Anthropic / Azure OpenAI / etc.).

Design constraints
------------------

* OTel packages are an **optional** dependency (the ``otel`` extra in
  ``pyproject.toml``). Every import here is guarded; the module
  degrades to no-op stubs when the packages are not installed.
* :func:`init_tracing` never raises. If the SDK is missing or
  ``OTEL_EXPORTER_OTLP_ENDPOINT`` is not set, it logs a debug note and
  returns. Operators point the env var at any OTLP-aware backend
  (the in-cluster collector, Phoenix, Datadog, LangSmith).
* :func:`agent_span` always returns a context manager yielding a
  span-like handle, even when OTel is unavailable, so call sites stay
  branch-free.

Usage
-----

    from caretaker.observability import bootstrap_observability, agent_span

    bootstrap_observability("caretaker-mcp")  # prefer this over init_tracing

    with agent_span(agent_name="pr", operation="run") as span:
        span.set_attribute("caretaker.run_id", run_id)
        await agent.run(...)
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator

logger = logging.getLogger(__name__)

# ── Optional OTel import guard ────────────────────────────────────────
#
# The SDK lives in the ``otel`` extra. When it's not installed we fall
# back to :class:`_NullSpan` so call sites can use ``with agent_span(...)``
# unconditionally.

# OTel symbols are typed as ``Any`` so the module imports cleanly even
# when the optional ``otel`` extra isn't installed and so call sites that
# stamp ad-hoc attributes (e.g. our hex ``trace_id``) onto the live span
# don't fight strict mypy.
_otel_trace: Any
_OTLPSpanExporter: Any
_OTelResource: Any
_TracerProvider: Any
_BatchSpanProcessor: Any

try:  # pragma: no cover - exercised by the fallback path in tests
    from opentelemetry import trace as _otel_trace  # type: ignore[no-redef, unused-ignore]
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (  # type: ignore[no-redef, unused-ignore]
        OTLPSpanExporter as _OTLPSpanExporter,
    )
    from opentelemetry.sdk.resources import (  # type: ignore[no-redef, unused-ignore]
        Resource as _OTelResource,
    )
    from opentelemetry.sdk.trace import (  # type: ignore[no-redef, unused-ignore]
        TracerProvider as _TracerProvider,
    )
    from opentelemetry.sdk.trace.export import (  # type: ignore[no-redef, unused-ignore]
        BatchSpanProcessor as _BatchSpanProcessor,
    )

    _OTEL_AVAILABLE = True
except ImportError:  # pragma: no cover - trivially true when OTel missing
    _otel_trace = None
    _OTLPSpanExporter = None
    _OTelResource = None
    _TracerProvider = None
    _BatchSpanProcessor = None

    _OTEL_AVAILABLE = False


# Process-wide idempotency flag. :func:`init_tracing` is cheap to call
# repeatedly (tests, hot reload, multiple FastAPI lifespans) — we only
# wire the TracerProvider once.
_TRACING_INITIALISED = False


class _NullSpan:
    """Stand-in span handle used when OTel is unavailable or unconfigured.

    Matches the subset of the OTel span API that caretaker call sites
    touch (``set_attribute`` + a string ``trace_id``) so the same
    ``with agent_span(...)`` block works in both modes.
    """

    __slots__ = ("trace_id",)

    def __init__(self) -> None:
        self.trace_id: str = ""

    def set_attribute(self, key: str, value: Any) -> None:  # noqa: ARG002
        return None


def _resolve_resource_attrs(service_name: str) -> dict[str, str]:
    """Build the OTel ``Resource`` attribute dict.

    Operator-supplied ``OTEL_SERVICE_NAME`` / ``OTEL_RESOURCE_ATTRIBUTES``
    win over caretaker defaults so deployments can override per-pod
    without code changes (matches the OTel SDK env-var contract).
    """
    attrs: dict[str, str] = {
        "service.name": os.environ.get("OTEL_SERVICE_NAME", "").strip() or service_name,
        "service.namespace": "caretaker",
    }

    # Pod name from the k8s downward API (set in our Deployment spec)
    # is the natural service.instance.id. Falls back to the local
    # hostname so non-k8s callers still get a useful value.
    instance_id = os.environ.get("HOSTNAME", "").strip()
    if not instance_id:
        try:
            import socket

            instance_id = socket.gethostname()
        except Exception:  # pragma: no cover - hostname is always available in practice
            instance_id = ""
    if instance_id:
        attrs["service.instance.id"] = instance_id

    # Pin the package version so traces from a partial rollout can be
    # cleanly bisected. Best-effort — never block startup if importlib
    # metadata can't find the dist (e.g. running from a source checkout
    # without ``pip install -e .``).
    try:
        import contextlib
        from importlib.metadata import PackageNotFoundError, version

        with contextlib.suppress(PackageNotFoundError):
            attrs["service.version"] = version("caretaker-github")
    except Exception:  # pragma: no cover
        pass

    # Operator-supplied OTEL_RESOURCE_ATTRIBUTES merges last so it can
    # override anything above (e.g. deployment.environment=staging vs prod).
    raw = os.environ.get("OTEL_RESOURCE_ATTRIBUTES", "").strip()
    if raw:
        for entry in raw.split(","):
            if "=" not in entry:
                continue
            key, _, value = entry.partition("=")
            key = key.strip()
            value = value.strip()
            if key:
                attrs[key] = value

    return attrs


def init_tracing(service_name: str = "caretaker") -> None:
    """Configure the global OTel tracer provider once per process.

    No-op when:

    * the ``otel`` extra is not installed, or
    * ``OTEL_EXPORTER_OTLP_ENDPOINT`` is not set.

    Never raises — tracing is strictly additive. A misconfigured OTel
    backend must not break an agent run.
    """
    global _TRACING_INITIALISED  # noqa: PLW0603 - idempotent singleton guard
    if _TRACING_INITIALISED:
        return
    if not _OTEL_AVAILABLE:
        logger.debug("OTel SDK not installed; tracing disabled")
        return

    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    if not endpoint:
        logger.debug("OTEL_EXPORTER_OTLP_ENDPOINT not set; tracing disabled")
        return

    try:
        assert _OTelResource is not None  # narrowing for mypy
        assert _TracerProvider is not None
        assert _BatchSpanProcessor is not None
        assert _OTLPSpanExporter is not None
        assert _otel_trace is not None

        resource = _OTelResource.create(_resolve_resource_attrs(service_name))
        provider = _TracerProvider(resource=resource)
        exporter = _OTLPSpanExporter(endpoint=endpoint)
        provider.add_span_processor(_BatchSpanProcessor(exporter))
        _otel_trace.set_tracer_provider(provider)

        # Lock the propagator contract: W3C tracecontext + baggage. The
        # SDK default is identical, but pinning it explicitly means a
        # stray ``OTEL_PROPAGATORS=jaeger`` in the environment can't
        # silently desync our producer/consumer trace context across the
        # event-bus hop.
        try:
            from opentelemetry.propagate import set_global_textmap
            from opentelemetry.propagators.composite import CompositePropagator
            from opentelemetry.trace.propagation.tracecontext import (
                TraceContextTextMapPropagator,
            )

            try:
                from opentelemetry.baggage.propagation import W3CBaggagePropagator

                set_global_textmap(
                    CompositePropagator([TraceContextTextMapPropagator(), W3CBaggagePropagator()])
                )
            except ImportError:  # pragma: no cover - baggage available in modern api
                set_global_textmap(TraceContextTextMapPropagator())
        except Exception:  # pragma: no cover
            logger.debug("Failed to pin OTel propagators; using SDK defaults", exc_info=True)

        _TRACING_INITIALISED = True
        logger.info(
            "OpenTelemetry tracing initialised (service=%s, endpoint=%s)",
            service_name,
            endpoint,
        )
    except Exception:
        # Never propagate — tracing failures must not cascade into the
        # orchestrator. Log at warning so operators can notice in prod.
        logger.warning("Failed to initialise OpenTelemetry tracing", exc_info=True)


@contextmanager
def agent_span(agent_name: str, operation: str) -> Iterator[Any]:
    """Yield a span handle for a single agent invocation.

    Span name is always ``invoke_agent`` per the GenAI semantic
    conventions (April 2026 snapshot). Attributes:

    * ``gen_ai.agent.name`` — the caretaker agent name (``pr``, ``issue``, …).
    * ``gen_ai.operation.name`` — typically ``run`` for agent dispatch.

    Yields either a real OTel span (when the SDK is configured) or a
    :class:`_NullSpan` stub so call sites stay branch-free.
    """
    if not _OTEL_AVAILABLE or _otel_trace is None:
        yield _NullSpan()
        return

    tracer = _otel_trace.get_tracer("caretaker.agents")
    with tracer.start_as_current_span("invoke_agent") as span:
        try:
            span.set_attribute("gen_ai.agent.name", agent_name)
            span.set_attribute("gen_ai.operation.name", operation)
            ctx = span.get_span_context()
            # OTel exposes ids as ints; convert to the hex strings used
            # by every trace backend so caretaker can cross-reference
            # spans without a second conversion at the call site.
            trace_id_int = getattr(ctx, "trace_id", 0) if ctx is not None else 0
            if trace_id_int:
                span.trace_id = f"{trace_id_int:032x}"
            else:  # pragma: no cover - defensive; trace_id should always exist
                span.trace_id = ""
        except Exception:
            # Never fail the agent run because of a span attribute.
            logger.debug("Failed to stamp agent span attributes", exc_info=True)
        yield span


def current_span_ids() -> tuple[str | None, str | None]:
    """Return ``(span_id, parent_span_id)`` for the active span, or ``(None, None)``.

    Used when constructing a :class:`~caretaker.causal_chain.CausalEvent`
    so the event carries the span provenance, letting ``(:CausalEvent)``
    nodes cross-link to the trace backend via ``span_id``.
    """
    if not _OTEL_AVAILABLE or _otel_trace is None:
        return (None, None)

    try:
        span = _otel_trace.get_current_span()
        if span is None:
            return (None, None)
        ctx = span.get_span_context()
        span_id_int = getattr(ctx, "span_id", 0) if ctx is not None else 0
        # A non-recording span has span_id == 0; treat that as "no span".
        if not span_id_int:
            return (None, None)
        span_id = f"{span_id_int:016x}"
        parent_span_id: str | None = None
        # The parent span id isn't directly exposed on the context; read
        # it off the SDK span object when available. Fall back to None
        # for non-SDK spans (e.g. a pure API NoOpSpan).
        parent_ctx = getattr(span, "parent", None)
        parent_span_id_int = getattr(parent_ctx, "span_id", 0) if parent_ctx is not None else 0
        if parent_span_id_int:
            parent_span_id = f"{parent_span_id_int:016x}"
        return (span_id, parent_span_id)
    except Exception:
        logger.debug("Failed to read current span ids", exc_info=True)
        return (None, None)


__all__ = [
    "_OTEL_AVAILABLE",
    "_NullSpan",
    "agent_span",
    "current_span_ids",
    "init_tracing",
]
