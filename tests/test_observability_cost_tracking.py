"""Tests for the LLM token + USD cost tracking helpers (observability phase 3).

Covers the three new helpers in :mod:`caretaker.observability.metrics`:

  * :func:`record_llm_tokens` — increments ``caretaker_llm_tokens_total``
    split by ``direction`` (``prompt`` / ``completion``).
  * :func:`record_llm_cost` — looks the model up in
    :data:`caretaker.config.LLM_PRICE_TABLE` and increments
    ``caretaker_llm_cost_usd_total``. Unknown models log a one-shot
    warning and skip the cost increment.
  * :func:`record_llm_usage` — convenience that fires both above.

Each assertion reads the underlying counter via
``REGISTRY.get_sample_value`` so the test exercises the same path
Prometheus uses on a scrape.
"""

from __future__ import annotations

import logging

import pytest

from caretaker.observability import metrics as metrics_mod
from caretaker.observability.metrics import (
    REGISTRY,
    get_service_label,
    record_llm_cost,
    record_llm_tokens,
    record_llm_usage,
)


def _service() -> str:
    return get_service_label()


def _read_tokens(model: str, direction: str) -> float:
    val = REGISTRY.get_sample_value(
        "caretaker_llm_tokens_total",
        {"service": _service(), "model": model, "direction": direction},
    )
    return 0.0 if val is None else float(val)


def _read_cost(model: str) -> float:
    val = REGISTRY.get_sample_value(
        "caretaker_llm_cost_usd_total",
        {"service": _service(), "model": model},
    )
    return 0.0 if val is None else float(val)


# ── record_llm_tokens ─────────────────────────────────────────────────


def test_record_llm_tokens_increments_counter() -> None:
    model = "openrouter/test/tokens-model"
    before_p = _read_tokens(model, "prompt")
    before_c = _read_tokens(model, "completion")
    record_llm_tokens(model, prompt_tokens=1234, completion_tokens=567)
    assert _read_tokens(model, "prompt") == pytest.approx(before_p + 1234)
    assert _read_tokens(model, "completion") == pytest.approx(before_c + 567)


def test_record_llm_tokens_skips_non_positive_values() -> None:
    """Zero/negative counts are no-ops so partial fanout calls don't pollute the counter."""
    model = "openrouter/test/zeros-model"
    before_p = _read_tokens(model, "prompt")
    before_c = _read_tokens(model, "completion")
    record_llm_tokens(model, prompt_tokens=0, completion_tokens=0)
    record_llm_tokens(model, prompt_tokens=-5, completion_tokens=-1)
    assert _read_tokens(model, "prompt") == pytest.approx(before_p)
    assert _read_tokens(model, "completion") == pytest.approx(before_c)


# ── record_llm_cost ───────────────────────────────────────────────────


def test_record_llm_cost_with_known_model(monkeypatch) -> None:
    """A known model with simple round-numbered prices yields the expected USD total.

    Price (1.00 input, 2.00 output) per 1M tokens, fed prompt=1_000_000
    + completion=500_000 → expect $1.00 (input) + $1.00 (output) =
    $2.00 increment.
    """
    test_model = "openrouter/test/known-pricing"
    monkeypatch.setitem(
        __import__("caretaker.config", fromlist=["LLM_PRICE_TABLE"]).LLM_PRICE_TABLE,
        test_model,
        (1.00, 2.00),
    )

    before = _read_cost(test_model)
    record_llm_cost(test_model, prompt_tokens=1_000_000, completion_tokens=500_000)
    after = _read_cost(test_model)
    assert after - before == pytest.approx(2.00, abs=1e-9)


def test_record_llm_cost_unknown_model_skips_and_warns(caplog, monkeypatch) -> None:
    """An unknown model logs a one-shot warning-level message and skips the cost increment."""
    # Reset the warn-once memo so this test is independent of run order.
    monkeypatch.setattr(metrics_mod, "_LLM_PRICE_TABLE_MISSES_WARNED", set())

    unknown_model = "openrouter/test/never-priced-model-xyz"
    before = _read_cost(unknown_model)
    with caplog.at_level(logging.WARNING, logger=metrics_mod.logger.name):
        record_llm_cost(unknown_model, prompt_tokens=10_000, completion_tokens=2_000)
        # Second call must NOT log again (warn-once memoisation).
        record_llm_cost(unknown_model, prompt_tokens=99, completion_tokens=99)
    after = _read_cost(unknown_model)

    # Cost counter unchanged.
    assert after == pytest.approx(before)

    # Exactly one log record for this model, at WARNING level.
    matching = [r for r in caplog.records if unknown_model in r.getMessage()]
    assert len(matching) == 1, f"expected 1 log for unknown model, got {len(matching)}"
    assert matching[0].levelno == logging.WARNING
    assert "missing from LLM_PRICE_TABLE" in matching[0].getMessage()


def test_record_llm_cost_skips_when_total_cost_is_zero(monkeypatch) -> None:
    """Zero-token call produces zero cost; counter stays unchanged."""
    test_model = "openrouter/test/zero-cost"
    monkeypatch.setitem(
        __import__("caretaker.config", fromlist=["LLM_PRICE_TABLE"]).LLM_PRICE_TABLE,
        test_model,
        (1.00, 2.00),
    )
    before = _read_cost(test_model)
    record_llm_cost(test_model, prompt_tokens=0, completion_tokens=0)
    assert _read_cost(test_model) == pytest.approx(before)


# ── record_llm_usage ──────────────────────────────────────────────────


def test_record_llm_usage_combined_increments_both_counters(monkeypatch) -> None:
    """``record_llm_usage`` fires both token + cost counters in one call."""
    test_model = "openrouter/test/combined-usage"
    monkeypatch.setitem(
        __import__("caretaker.config", fromlist=["LLM_PRICE_TABLE"]).LLM_PRICE_TABLE,
        test_model,
        (3.00, 6.00),  # USD per 1M
    )

    before_p = _read_tokens(test_model, "prompt")
    before_c = _read_tokens(test_model, "completion")
    before_cost = _read_cost(test_model)

    record_llm_usage(test_model, prompt_tokens=2_000_000, completion_tokens=1_000_000)

    # Tokens recorded.
    assert _read_tokens(test_model, "prompt") == pytest.approx(before_p + 2_000_000)
    assert _read_tokens(test_model, "completion") == pytest.approx(before_c + 1_000_000)
    # Cost: 2M * $3/M + 1M * $6/M = $6 + $6 = $12.
    assert _read_cost(test_model) - before_cost == pytest.approx(12.0, abs=1e-9)


def test_record_llm_usage_unknown_model_records_tokens_skips_cost(monkeypatch) -> None:
    """Unknown model: token counter still moves, cost counter does not."""
    monkeypatch.setattr(metrics_mod, "_LLM_PRICE_TABLE_MISSES_WARNED", set())
    unknown_model = "openrouter/test/usage-no-price"

    before_p = _read_tokens(unknown_model, "prompt")
    before_c = _read_tokens(unknown_model, "completion")
    before_cost = _read_cost(unknown_model)

    record_llm_usage(unknown_model, prompt_tokens=1_000, completion_tokens=200)

    assert _read_tokens(unknown_model, "prompt") == pytest.approx(before_p + 1_000)
    assert _read_tokens(unknown_model, "completion") == pytest.approx(before_c + 200)
    assert _read_cost(unknown_model) == pytest.approx(before_cost)


# ── price-table coverage ──────────────────────────────────────────────


def test_llm_price_table_covers_v028_default_models() -> None:
    """All v0.28.x default models should be in the price table.

    If this fails after adding a new default model, add the model to
    ``LLM_PRICE_TABLE`` in ``caretaker.config`` rather than dropping it
    from the assertion list.
    """
    from caretaker.config import LLM_PRICE_TABLE

    expected = {
        # review_models defaults
        "openrouter/google/gemini-2.5-flash-lite",
        "openrouter/google/gemini-2.5-flash",
        "openrouter/deepseek/deepseek-v4-flash",
        "openrouter/google/gemini-2.5-pro",
        # fix_models defaults
        "openrouter/anthropic/claude-haiku-4.5",
        "openrouter/deepseek/deepseek-v4-pro",
        "openrouter/anthropic/claude-sonnet-4.5",
    }
    missing = expected - set(LLM_PRICE_TABLE)
    assert not missing, f"v0.28 default models missing from price table: {missing}"
