"""Tests for per-site model-override plumbing in ``@shadow_decision``.

Covers the PR #503 follow-up: :class:`AgenticDomainConfig.model_override`
/ ``max_tokens_override`` must flow into the candidate leg of the
decorator via a ``model=`` kwarg, and both models (legacy + candidate)
must land on the written :class:`ShadowDecisionRecord` so the paired
Braintrust experiments can tell a prompt-change diff from a
model-swap diff.

The tests are deliberately paranoid about state leakage: two sites
with different overrides must not see each other's kwargs, even when
invoked back-to-back. We assert the candidate-observed ``model`` kwarg
directly rather than poking at the LLM transport — the decorator's
contract is the kwarg injection; downstream propagation is covered by
the existing ``tests/test_pr_agent/test_readiness_llm.py`` suite.
"""

from __future__ import annotations

from typing import Any

import pytest

from caretaker.config import (
    AgenticConfig,
    AgenticDomainConfig,
    LLMConfig,
    MaintainerConfig,
)
from caretaker.evolution import shadow_config
from caretaker.evolution.shadow import (
    clear_records_for_tests,
    recent_records,
    shadow_decision,
)


@pytest.fixture(autouse=True)
def _reset_shadow_state() -> None:
    """Clear cross-test state (ring buffer, active config)."""
    clear_records_for_tests()
    shadow_config.reset_for_tests()


# ── Helpers ──────────────────────────────────────────────────────────────


def _install_maintainer(
    *,
    default_model: str = "claude-sonnet-4-5",
    **domain_overrides: AgenticDomainConfig,
) -> None:
    """Install a full :class:`MaintainerConfig` into the shadow resolver.

    Keyword args are a ``site_name → AgenticDomainConfig`` map; unlisted
    sites stay at the ``mode="off"`` default, which the decorator
    interprets as "do not run candidate".
    """
    agentic = AgenticConfig(**domain_overrides)  # type: ignore[arg-type]
    maintainer = MaintainerConfig(
        llm=LLMConfig(default_model=default_model),
        agentic=agentic,
    )
    shadow_config.configure_maintainer(maintainer)


# ── 1. Unset override → candidate sees default_model ──────────────────────


async def test_unset_override_stamps_default_model_on_record() -> None:
    _install_maintainer(
        default_model="claude-sonnet-4-5",
        readiness=AgenticDomainConfig(mode="shadow"),
    )

    observed: dict[str, Any] = {}

    async def legacy() -> str:
        return "OK"

    async def candidate(**kwargs: Any) -> str:
        # The decorator only injects ``model`` / ``max_tokens`` when the
        # override is set; an unset override must leave kwargs clean.
        observed.update(kwargs)
        return "OK"

    @shadow_decision("readiness")
    async def decide(*, legacy: Any, candidate: Any, context: Any = None) -> str:
        raise AssertionError("wrapper drives")

    result = await decide(legacy=legacy, candidate=candidate)
    assert result == "OK"

    # Decorator did NOT inject a model kwarg for the unset override.
    assert "model" not in observed
    assert "max_tokens" not in observed

    records = recent_records()
    assert len(records) == 1
    rec = records[0]
    # Candidate model falls back to ``llm.default_model`` — the record
    # must carry it so paired experiments can reason about the split.
    assert rec.candidate_model == "claude-sonnet-4-5"
    assert rec.legacy_model == "claude-sonnet-4-5"


# ── 2. Set override → candidate sees override + record stamps it ──────────


async def test_override_flows_to_candidate_kwargs_and_record() -> None:
    _install_maintainer(
        default_model="claude-sonnet-4-5",
        readiness=AgenticDomainConfig(
            mode="shadow",
            model_override="azure_ai/claude-sonnet-4",
            max_tokens_override=1200,
        ),
    )

    observed: dict[str, Any] = {}

    async def legacy() -> str:
        return "OK"

    async def candidate(**kwargs: Any) -> str:
        observed.update(kwargs)
        return "OK"

    @shadow_decision("readiness")
    async def decide(*, legacy: Any, candidate: Any, context: Any = None) -> str:
        raise AssertionError("wrapper drives")

    await decide(legacy=legacy, candidate=candidate)

    # The candidate leg must receive the override so its downstream
    # ``ClaudeClient.structured_complete`` call can forward the model.
    assert observed.get("model") == "azure_ai/claude-sonnet-4"
    assert observed.get("max_tokens") == 1200

    records = recent_records()
    assert len(records) == 1
    rec = records[0]
    assert rec.candidate_model == "azure_ai/claude-sonnet-4"
    # Legacy leg keeps running on whatever the router's default was —
    # the override affects only the candidate.
    assert rec.legacy_model == "claude-sonnet-4-5"


# ── 3. Per-site isolation — overrides must not leak ───────────────────────


async def test_two_sites_with_distinct_overrides_do_not_leak() -> None:
    """Two decisions from different sites run back-to-back without leaks.

    This is the "no module-global state for the override" assertion
    called out in the PR plan: both sites must resolve their own
    override independently, even when the decorator is exercised twice
    from the same async task.
    """
    _install_maintainer(
        default_model="claude-sonnet-4-5",
        readiness=AgenticDomainConfig(
            mode="shadow",
            model_override="azure_ai/claude-sonnet-4",
        ),
        ci_triage=AgenticDomainConfig(
            mode="shadow",
            model_override="azure_ai/gpt-5",
        ),
    )

    readiness_observed: dict[str, Any] = {}
    ci_observed: dict[str, Any] = {}

    async def readiness_legacy() -> str:
        return "ready"

    async def readiness_candidate(**kwargs: Any) -> str:
        readiness_observed.update(kwargs)
        return "ready"

    async def ci_legacy() -> str:
        return "flaky"

    async def ci_candidate(**kwargs: Any) -> str:
        ci_observed.update(kwargs)
        return "flaky"

    @shadow_decision("readiness")
    async def decide_readiness(*, legacy: Any, candidate: Any, context: Any = None) -> str:
        raise AssertionError("wrapper drives")

    @shadow_decision("ci_triage")
    async def decide_ci(*, legacy: Any, candidate: Any, context: Any = None) -> str:
        raise AssertionError("wrapper drives")

    await decide_readiness(legacy=readiness_legacy, candidate=readiness_candidate)
    await decide_ci(legacy=ci_legacy, candidate=ci_candidate)

    # Each site saw ONLY its own override, never the sibling's.
    assert readiness_observed["model"] == "azure_ai/claude-sonnet-4"
    assert ci_observed["model"] == "azure_ai/gpt-5"

    # Ring buffer has two rows, one per site, each with its own model.
    records = recent_records()
    by_site = {r.name: r for r in records}
    assert by_site["readiness"].candidate_model == "azure_ai/claude-sonnet-4"
    assert by_site["ci_triage"].candidate_model == "azure_ai/gpt-5"


# ── 4. Enforce-mode path also threads the override ───────────────────────


async def test_enforce_mode_candidate_also_sees_override() -> None:
    _install_maintainer(
        default_model="claude-sonnet-4-5",
        readiness=AgenticDomainConfig(
            mode="enforce",
            model_override="azure_ai/claude-sonnet-4",
        ),
    )

    observed: dict[str, Any] = {}

    async def legacy() -> str:
        return "LEGACY"

    async def candidate(**kwargs: Any) -> str:
        observed.update(kwargs)
        return "CANDIDATE"

    @shadow_decision("readiness")
    async def decide(*, legacy: Any, candidate: Any, context: Any = None) -> str:
        raise AssertionError("wrapper drives")

    result = await decide(legacy=legacy, candidate=candidate)
    assert result == "CANDIDATE"
    assert observed["model"] == "azure_ai/claude-sonnet-4"


# ── 5. Candidate error path still stamps models on the record ────────────


async def test_candidate_error_records_model_fields() -> None:
    _install_maintainer(
        default_model="claude-sonnet-4-5",
        readiness=AgenticDomainConfig(
            mode="shadow",
            model_override="azure_ai/claude-sonnet-4",
        ),
    )

    async def legacy() -> str:
        return "OK"

    async def candidate(**kwargs: Any) -> str:
        raise RuntimeError("boom")

    @shadow_decision("readiness")
    async def decide(*, legacy: Any, candidate: Any, context: Any = None) -> str:
        raise AssertionError("wrapper drives")

    result = await decide(legacy=legacy, candidate=candidate)
    assert result == "OK"  # shadow returns legacy even on candidate error

    records = recent_records()
    assert len(records) == 1
    rec = records[0]
    assert rec.outcome == "candidate_error"
    # Even though the candidate leg crashed, we still know *which* model
    # it was meant to use — the audit trail shouldn't lose that just
    # because the LLM raised.
    assert rec.candidate_model == "azure_ai/claude-sonnet-4"
    assert rec.legacy_model == "claude-sonnet-4-5"


# ── 6. No MaintainerConfig installed → models resolve to None ─────────────


async def test_no_maintainer_config_leaves_models_none() -> None:
    """When only :func:`shadow_config.configure` is used (legacy test path).

    The bare-``AgenticConfig`` resolver path is the one every existing
    test hits, so this case must not regress to writing garbage or
    raising: stamping ``None`` is the correct fallback.
    """
    shadow_config.configure(AgenticConfig(readiness=AgenticDomainConfig(mode="shadow")))

    async def legacy() -> str:
        return "OK"

    async def candidate(**kwargs: Any) -> str:
        # No override configured via ``configure_maintainer`` → no kwarg.
        assert "model" not in kwargs
        return "OK"

    @shadow_decision("readiness")
    async def decide(*, legacy: Any, candidate: Any, context: Any = None) -> str:
        raise AssertionError("wrapper drives")

    await decide(legacy=legacy, candidate=candidate)

    records = recent_records()
    assert len(records) == 1
    assert records[0].candidate_model is None
    assert records[0].legacy_model is None
