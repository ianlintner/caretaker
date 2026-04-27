"""Tests for OpenTelemetry GenAI agent-span instrumentation (M8).

The observability helpers ship as an **optional** install — the OTel
SDK lives in the ``otel`` extra. These tests cover the graceful
fallback path (``_NullSpan`` stub, no-op ``init_tracing``) so the
default install stays safe, and the SDK-present path when the
packages are available on the test runner.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

import pytest

from caretaker.observability import otel as otel_module
from caretaker.observability.otel import (
    _OTEL_AVAILABLE,
    _NullSpan,
    agent_span,
    current_span_ids,
    init_tracing,
)

if TYPE_CHECKING:
    from pytest import MonkeyPatch


@pytest.fixture(autouse=True)
def _reset_tracing_flag() -> None:
    """Reset the process-wide idempotency guard between tests."""
    otel_module._TRACING_INITIALISED = False
    yield
    otel_module._TRACING_INITIALISED = False


class TestInitTracing:
    def test_noop_when_endpoint_unset(self, monkeypatch: MonkeyPatch) -> None:
        """``init_tracing`` must be a no-op when the env var is unset.

        Default install path — operators opt in by setting
        ``OTEL_EXPORTER_OTLP_ENDPOINT``.
        """
        monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
        init_tracing("caretaker-test")
        assert otel_module._TRACING_INITIALISED is False

    def test_noop_when_endpoint_blank(self, monkeypatch: MonkeyPatch) -> None:
        """Whitespace-only endpoint is treated the same as unset."""
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "   ")
        init_tracing("caretaker-test")
        assert otel_module._TRACING_INITIALISED is False

    def test_never_raises(self, monkeypatch: MonkeyPatch) -> None:
        """``init_tracing`` swallows any configuration error."""
        monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
        # Multiple calls are idempotent even from the no-op branch.
        init_tracing("caretaker-test")
        init_tracing("caretaker-test")

    @pytest.mark.skipif(not _OTEL_AVAILABLE, reason="OTel SDK not installed")
    def test_configured_endpoint_does_not_raise(self, monkeypatch: MonkeyPatch) -> None:
        """With the SDK installed, a valid endpoint should init cleanly.

        The exporter is lazily-bound (it only ships spans on flush), so
        pointing at a non-listening host is safe for an init-time test.
        """
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:14317")
        # Force a fresh module import so any module-level cached state
        # from earlier tests is dropped.
        importlib.reload(otel_module)
        otel_module._TRACING_INITIALISED = False
        otel_module.init_tracing("caretaker-test")
        assert otel_module._TRACING_INITIALISED is True


class TestAgentSpan:
    def test_yields_null_span_when_otel_missing(self, monkeypatch: MonkeyPatch) -> None:
        """Call sites must not have to branch on OTel availability.

        ``agent_span`` yields something with a string ``trace_id`` and
        a ``set_attribute`` method even when OTel is not initialised.
        """
        monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
        with agent_span(agent_name="pr", operation="run") as span:
            assert hasattr(span, "trace_id")
            assert isinstance(span.trace_id, str)
            # set_attribute must not raise.
            span.set_attribute("caretaker.run_id", "run-123")

    def test_null_span_set_attribute_accepts_any_value(self) -> None:
        """The stub must accept the same signature as a real span."""
        null = _NullSpan()
        assert null.trace_id == ""
        null.set_attribute("k", "v")
        null.set_attribute("k", 42)
        null.set_attribute("k", None)

    def test_context_manager_always_exits_cleanly(self, monkeypatch: MonkeyPatch) -> None:
        """Exceptions inside the block must propagate; no suppression."""
        monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)

        with pytest.raises(RuntimeError, match="boom"), agent_span("pr", "run"):
            raise RuntimeError("boom")


class TestCurrentSpanIds:
    def test_returns_none_outside_span(self, monkeypatch: MonkeyPatch) -> None:
        """No active span → ``(None, None)``.

        Callers (CausalEvent construction) rely on this so a causal
        marker extracted outside a span context still produces a valid
        event.
        """
        monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
        assert current_span_ids() == (None, None)
