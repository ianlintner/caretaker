"""Manual GenAI spans around LLM provider calls.

The ``opentelemetry-instrumentation-httpx`` instrumentor we ship in
the ``otel`` extra emits a span for the underlying HTTP roundtrip,
but it has no visibility into model name, tokens, or finish reason —
those live above the wire. We add a thin wrapper that emits a
``chat`` span carrying the OTel GenAI semantic-convention attributes
(April 2026 snapshot) so trace backends can show:

* ``gen_ai.system`` — provider name (``anthropic``, ``openai``, ``azure.openai``).
* ``gen_ai.request.model`` / ``gen_ai.response.model``.
* ``gen_ai.request.max_tokens`` / ``gen_ai.request.temperature``.
* ``gen_ai.response.id`` / ``gen_ai.response.finish_reasons``.
* ``gen_ai.usage.input_tokens`` / ``gen_ai.usage.output_tokens``.

Failures get :func:`Span.record_exception` + ``StatusCode.ERROR`` so
the cluster collector's ``keep-errors`` tail-sampling policy retains
them even when the trace would otherwise be probabilistically dropped.

Usage
-----

    with llm_chat_span(
        system="anthropic",
        model=request.model,
        max_tokens=request.max_tokens,
    ) as span:
        response = await client.messages.create(...)
        span.record_response(
            model=response.model,
            response_id=response.id,
            finish_reasons=[response.stop_reason],
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )

When the OTel SDK is missing or unconfigured, the context manager
yields a no-op stub so call sites never have to branch on availability.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator

logger = logging.getLogger(__name__)


# Optional OTel imports — mirrors observability.otel.
_otel_trace: Any

try:  # pragma: no cover
    from opentelemetry import trace as _otel_trace  # type: ignore[no-redef, unused-ignore]

    _OTEL_AVAILABLE = True
except ImportError:  # pragma: no cover
    _otel_trace = None
    _OTEL_AVAILABLE = False


class _NullLLMSpan:
    """Stand-in returned when OTel isn't configured. Exposes the same API."""

    __slots__ = ()

    def set_attribute(self, key: str, value: Any) -> None:  # noqa: ARG002
        return None

    def record_response(
        self,
        *,
        model: str | None = None,
        response_id: str | None = None,
        finish_reasons: list[str] | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
    ) -> None:
        return None


class _LLMSpan:
    """Wrapper around a real OTel span exposing ``record_response``."""

    __slots__ = ("_span",)

    def __init__(self, span: Any) -> None:
        self._span = span

    def set_attribute(self, key: str, value: Any) -> None:
        try:
            self._span.set_attribute(key, value)
        except Exception:
            logger.debug("Failed to set LLM span attribute %s", key, exc_info=True)

    def record_response(
        self,
        *,
        model: str | None = None,
        response_id: str | None = None,
        finish_reasons: list[str] | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
    ) -> None:
        try:
            if model:
                self._span.set_attribute("gen_ai.response.model", model)
            if response_id:
                self._span.set_attribute("gen_ai.response.id", response_id)
            if finish_reasons:
                # Spec wants an array; OTel API accepts list of str.
                self._span.set_attribute(
                    "gen_ai.response.finish_reasons", [str(r) for r in finish_reasons if r]
                )
            if input_tokens is not None:
                self._span.set_attribute("gen_ai.usage.input_tokens", int(input_tokens))
            if output_tokens is not None:
                self._span.set_attribute("gen_ai.usage.output_tokens", int(output_tokens))
        except Exception:
            logger.debug("Failed to record LLM response attributes", exc_info=True)


@contextmanager
def llm_chat_span(
    *,
    system: str,
    model: str,
    operation: str = "chat",
    max_tokens: int | None = None,
    temperature: float | None = None,
    extra_attrs: dict[str, Any] | None = None,
) -> Iterator[_LLMSpan | _NullLLMSpan]:
    """Yield a span for one LLM completion call.

    Span name follows the GenAI convention: ``"<operation> <model>"``.
    Failures inside the ``with`` block are recorded with the OTel
    exception-event API and re-raised — the caller sees normal
    exception flow.
    """
    if not _OTEL_AVAILABLE or _otel_trace is None:
        yield _NullLLMSpan()
        return

    tracer = _otel_trace.get_tracer("caretaker.llm")
    span_name = f"{operation} {model}" if model else operation
    with tracer.start_as_current_span(span_name) as raw_span:
        wrapper = _LLMSpan(raw_span)
        try:
            wrapper.set_attribute("gen_ai.system", system)
            wrapper.set_attribute("gen_ai.operation.name", operation)
            if model:
                wrapper.set_attribute("gen_ai.request.model", model)
            if max_tokens is not None:
                wrapper.set_attribute("gen_ai.request.max_tokens", int(max_tokens))
            if temperature is not None:
                wrapper.set_attribute("gen_ai.request.temperature", float(temperature))
            if extra_attrs:
                for key, value in extra_attrs.items():
                    wrapper.set_attribute(key, value)
        except Exception:
            logger.debug("Failed to stamp LLM request attributes", exc_info=True)

        try:
            yield wrapper
        except Exception as exc:
            try:
                from opentelemetry.trace import Status, StatusCode

                raw_span.record_exception(exc)
                raw_span.set_status(Status(StatusCode.ERROR, str(exc)[:200]))
            except Exception:
                pass
            raise


__all__ = ["llm_chat_span"]
