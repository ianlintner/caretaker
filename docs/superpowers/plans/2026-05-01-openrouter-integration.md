# OpenRouter Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add OpenRouter as a recognized first-class LLM provider with per-feature model routing and a `:online` web-grounded default for upgrade/migration features.

**Architecture:** Additive on top of the existing `LiteLLMProvider` and `feature_models` config. No new provider class. `provider: openrouter` becomes a recognized alias in `build_provider`; a new `DEFAULT_FEATURE_MODELS_BY_PROVIDER` dict supplies provider-aware shipped defaults; strict pydantic validation ensures all OpenRouter configs use the `openrouter/` prefix. The existing doctor `_MODEL_PREFIX_ENV_MAP` infrastructure handles env-var validation with one tuple addition (simpler than the spec originally described — discovered during plan-writing, see Task 7).

**Tech Stack:** Python 3.12+, pydantic v2 (`StrictBaseModel` with `model_validator`), pytest, LiteLLM, OpenTelemetry GenAI semantic conventions.

**Spec:** [`docs/superpowers/specs/2026-05-01-openrouter-integration-design.md`](../specs/2026-05-01-openrouter-integration-design.md)

---

## File Structure

| File | Role | Action |
|------|------|--------|
| `src/caretaker/llm/provider.py` | LLM provider implementations & factory | Modify (Tasks 1, 3, 6) |
| `src/caretaker/config.py` | Pydantic config models including `LLMConfig` | Modify (Tasks 2, 4, 5) |
| `src/caretaker/llm/claude.py` | Feature-keyed client with `_resolve_feature` | Modify (Task 4) |
| `src/caretaker/doctor.py` | Preflight env-var checks via `_MODEL_PREFIX_ENV_MAP` | Modify (Task 7) |
| `tests/test_llm.py` | Provider, router, resolver tests | Modify (Tasks 1, 3, 4, 6) |
| `tests/test_config.py` | Config-validation tests | Modify (Tasks 2, 5) |
| `tests/test_doctor_llm_env.py` | Doctor env-ref tests | Modify (Task 7) |
| `README.md` | Top-level integration docs | Modify (Task 8) |
| `docs/configuration.md` | Full config schema reference | Modify (Task 8) |

---

## Task 1: Recognize `OPENROUTER_API_KEY` in `LiteLLMProvider.available`

**Why this task matters:** Today `LiteLLMProvider.available` returns False if the only credential set is `OPENROUTER_API_KEY`, even though LiteLLM itself routes `openrouter/*` model strings fine using that key. This is a latent bug independent of the larger integration; fixing it first unblocks every other task.

**Files:**
- Modify: `src/caretaker/llm/provider.py:317-330` (add one entry to the env-var allowlist)
- Test: `tests/test_llm.py` (add one test)

- [ ] **Step 1: Write the failing test**

Add to `tests/test_llm.py` near the other `LiteLLMProvider` `available`-property tests (around line 198):

```python
def test_litellm_provider_available_with_only_openrouter_key(monkeypatch):
    """LiteLLMProvider.available must return True when only OPENROUTER_API_KEY is set."""
    # Clear every other credential the allowlist checks, leaving only OpenRouter.
    for key in (
        "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "AZURE_API_KEY", "AZURE_AI_API_KEY",
        "VERTEX_PROJECT", "GOOGLE_APPLICATION_CREDENTIALS", "AWS_ACCESS_KEY_ID",
        "MISTRAL_API_KEY", "COHERE_API_KEY", "GROQ_API_KEY", "OLLAMA_API_BASE",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-test")

    p = build_provider("litellm")
    # Skip if litellm package isn't installed in the test env — the regression
    # we're guarding only matters when the package IS installed.
    if not getattr(p, "package_installed", False):
        pytest.skip("litellm package not installed")
    assert p.available is True
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
pytest tests/test_llm.py::test_litellm_provider_available_with_only_openrouter_key -v
```

Expected: FAIL — `assert False is True` because the env-var allowlist does not yet include `OPENROUTER_API_KEY`.

- [ ] **Step 3: Add `OPENROUTER_API_KEY` to the allowlist**

In `src/caretaker/llm/provider.py:317-330`, edit the tuple inside `LiteLLMProvider.available`:

```python
return any(
    os.environ.get(key)
    for key in (
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "AZURE_API_KEY",
        "AZURE_AI_API_KEY",
        "VERTEX_PROJECT",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "AWS_ACCESS_KEY_ID",
        "MISTRAL_API_KEY",
        "COHERE_API_KEY",
        "GROQ_API_KEY",
        "OLLAMA_API_BASE",
        "OPENROUTER_API_KEY",
    )
)
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
pytest tests/test_llm.py::test_litellm_provider_available_with_only_openrouter_key -v
```

Expected: PASS.

- [ ] **Step 5: Run the rest of the test_llm.py file to confirm no regressions**

```bash
pytest tests/test_llm.py -v
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/caretaker/llm/provider.py tests/test_llm.py
git commit -m "fix(llm): recognize OPENROUTER_API_KEY in LiteLLMProvider.available

Previously LiteLLMProvider.available returned False when the only
configured credential was OPENROUTER_API_KEY, even though LiteLLM
routes openrouter/* models fine using that key. Add it to the
env-var allowlist so OpenRouter-only configs activate the router."
```

---

## Task 2: Add `"openrouter"` to the `LLMConfig.provider` Literal

**Why this task matters:** `LLMConfig.provider` is currently `Literal["anthropic", "litellm"]`, so setting `provider: openrouter` in YAML raises a pydantic validation error before the factory even gets a chance to handle it. This widens the type so `provider: openrouter` becomes a valid config value.

**Files:**
- Modify: `src/caretaker/config.py:412` (change Literal)
- Test: `tests/test_config.py` (add one test)

- [ ] **Step 1: Write the failing test**

Add to `tests/test_config.py`:

```python
def test_llm_config_accepts_openrouter_provider():
    """LLMConfig must accept provider='openrouter' as a recognized value."""
    from caretaker.config import LLMConfig

    config = LLMConfig(provider="openrouter", default_model="openrouter/anthropic/claude-sonnet-4.6")
    assert config.provider == "openrouter"
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
pytest tests/test_config.py::test_llm_config_accepts_openrouter_provider -v
```

Expected: FAIL — pydantic raises `ValidationError: Input should be 'anthropic' or 'litellm'`.

- [ ] **Step 3: Widen the Literal**

In `src/caretaker/config.py:412`, change:

```python
provider: Literal["anthropic", "litellm"] = "anthropic"
```

to:

```python
# "openrouter" is an alias that resolves to LiteLLM under the hood but
# enforces openrouter/-prefixed model strings (see model_validator below).
provider: Literal["anthropic", "litellm", "openrouter"] = "anthropic"
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
pytest tests/test_config.py::test_llm_config_accepts_openrouter_provider -v
```

Expected: PASS.

- [ ] **Step 5: Run the rest of test_config.py for regressions**

```bash
pytest tests/test_config.py -v
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/caretaker/config.py tests/test_config.py
git commit -m "feat(config): accept provider='openrouter' on LLMConfig

Widens the provider Literal to include openrouter as a recognized
value. Routing behavior added in subsequent commit."
```

---

## Task 3: Add `"openrouter"` branch in `build_provider`

**Why this task matters:** The factory function constructs the right provider class per name. We add a branch that returns a `LiteLLMProvider` (no new class) but logs a clear, OpenRouter-specific warning when `OPENROUTER_API_KEY` is unset — operators see "OPENROUTER_API_KEY is not set" instead of the generic LiteLLM "no credentials" message.

**Files:**
- Modify: `src/caretaker/llm/provider.py:701-724` (`build_provider`)
- Test: `tests/test_llm.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_llm.py` near the other `build_provider` tests (around line 184):

```python
def test_build_provider_openrouter_returns_litellm(monkeypatch):
    """build_provider('openrouter') returns a LiteLLMProvider instance."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-test")
    p = build_provider("openrouter")
    if not getattr(p, "package_installed", False):
        pytest.skip("litellm package not installed")
    assert p.name == "litellm"
    assert isinstance(p, LiteLLMProvider)


def test_build_provider_openrouter_warns_when_key_missing(monkeypatch, caplog):
    """build_provider('openrouter') warns specifically about OPENROUTER_API_KEY."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with caplog.at_level("WARNING"):
        build_provider("openrouter")
    messages = " ".join(rec.getMessage() for rec in caplog.records)
    assert "OPENROUTER_API_KEY" in messages


def test_build_provider_openrouter_no_warning_when_key_present(monkeypatch, caplog):
    """No 'OPENROUTER_API_KEY' warning when the env var is set."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-test")
    with caplog.at_level("WARNING"):
        build_provider("openrouter")
    messages = " ".join(rec.getMessage() for rec in caplog.records)
    assert "OPENROUTER_API_KEY is not set" not in messages
```

Make sure `LiteLLMProvider` is in the import block at the top of `tests/test_llm.py` (alongside `build_provider`).

- [ ] **Step 2: Run the tests to verify they fail**

```bash
pytest tests/test_llm.py::test_build_provider_openrouter_returns_litellm tests/test_llm.py::test_build_provider_openrouter_warns_when_key_missing tests/test_llm.py::test_build_provider_openrouter_no_warning_when_key_present -v
```

Expected: all three FAIL — `build_provider("openrouter")` falls through the unknown-provider branch and returns a `NullProvider` with a generic warning.

- [ ] **Step 3: Add the openrouter branch**

In `src/caretaker/llm/provider.py:701-724`, modify `build_provider`:

```python
def build_provider(
    provider_name: str,
    *,
    timeout: float = 60.0,
    fallback_models: list[str] | None = None,
) -> LLMProvider:
    """Factory: construct a provider from its name.

    Unknown or explicitly disabled providers return a ``NullProvider``.
    """
    name = provider_name.lower().strip()
    if name == "anthropic":
        return AnthropicProvider(timeout=timeout)
    if name == "openrouter":
        # Alias: LiteLLM with explicit OpenRouter credential check. We log
        # a targeted warning rather than the generic "no credentials" one
        # so operators see the exact env var to set.
        if not os.environ.get("OPENROUTER_API_KEY"):
            logger.warning(
                "provider='openrouter' but OPENROUTER_API_KEY is not set; "
                "LLM features will fall back to their non-LLM paths"
            )
        provider = LiteLLMProvider(fallback_models=fallback_models, timeout=timeout)
        if not provider.package_installed:
            logger.warning(
                "Configured provider 'openrouter' but litellm package is not installed; "
                "install with `pip install litellm`"
            )
            return NullProvider()
        return provider
    if name == "litellm":
        provider = LiteLLMProvider(fallback_models=fallback_models, timeout=timeout)
        if not provider.package_installed:
            logger.warning(
                "Configured provider 'litellm' but package not installed; "
                "falling back to AnthropicProvider"
            )
            return AnthropicProvider(timeout=timeout)
        return provider
    logger.warning("Unknown LLM provider '%s'; using NullProvider", provider_name)
    return NullProvider()
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
pytest tests/test_llm.py -k "openrouter" -v
```

Expected: PASS.

- [ ] **Step 5: Run full test_llm.py for regressions**

```bash
pytest tests/test_llm.py -v
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/caretaker/llm/provider.py tests/test_llm.py
git commit -m "feat(llm): add openrouter provider alias to build_provider

provider='openrouter' constructs a LiteLLMProvider (no new class) and
logs a targeted warning when OPENROUTER_API_KEY is unset, so operators
see the exact env var to set rather than a generic credentials warning."
```

---

## Task 4: Provider-aware default feature models

**Why this task matters:** OpenRouter operators need shipped defaults that route specific features to specific best-fit OpenRouter models — including the `:online` suffix on three features. This is the heart of the "per-feature model diversity" goal. Anthropic operators must see no behavior change.

**Files:**
- Modify: `src/caretaker/config.py:293-315` (add `DEFAULT_FEATURE_MODELS_BY_PROVIDER`)
- Modify: `src/caretaker/llm/claude.py:138-155` (`_resolve_feature`)
- Test: `tests/test_llm.py`

- [ ] **Step 1: Verify the OpenRouter model strings against the live catalog**

The spec uses `openrouter/anthropic/claude-opus-4.6`, `openrouter/anthropic/claude-sonnet-4.6:online`, and `openrouter/deepseek/deepseek-r1`. Visit `https://openrouter.ai/models?q=anthropic` and `https://openrouter.ai/models?q=deepseek` and confirm the exact model IDs. **If any string in the spec is wrong, update the spec doc first** — see [`docs/superpowers/specs/2026-05-01-openrouter-integration-design.md`](../specs/2026-05-01-openrouter-integration-design.md) — then proceed with the verified strings here.

Record what you found. The strings that ship in `DEFAULT_FEATURE_MODELS_BY_PROVIDER` must match the catalog exactly; LiteLLM forwards the model string verbatim and OpenRouter returns 404 on a typo.

- [ ] **Step 2: Write the failing tests**

Add to `tests/test_llm.py`:

```python
def test_resolve_feature_uses_openrouter_default_for_migration_analysis():
    """When provider='openrouter', migration_analysis resolves to the :online model."""
    from caretaker.llm.claude import ClaudeClient

    config = LLMConfig(
        provider="openrouter",
        default_model="openrouter/anthropic/claude-sonnet-4.6",
    )
    client = ClaudeClient(config=config)
    model, max_tokens = client._resolve_feature("migration_analysis", default_max_tokens=1)
    assert model == "openrouter/anthropic/claude-sonnet-4.6:online"
    assert max_tokens == 4000


def test_resolve_feature_uses_legacy_default_for_anthropic_provider():
    """When provider='anthropic', migration_analysis resolves to the legacy Claude default."""
    from caretaker.llm.claude import ClaudeClient
    from caretaker.config import DEFAULT_REASONING_MODEL

    config = LLMConfig(provider="anthropic")
    client = ClaudeClient(config=config)
    model, max_tokens = client._resolve_feature("migration_analysis", default_max_tokens=1)
    assert model == DEFAULT_REASONING_MODEL
    assert max_tokens == 4000


def test_resolve_feature_operator_override_beats_openrouter_default():
    """Operator's feature_models override is authoritative; :online is NOT re-appended."""
    from caretaker.llm.claude import ClaudeClient

    config = LLMConfig(
        provider="openrouter",
        default_model="openrouter/anthropic/claude-sonnet-4.6",
        feature_models={
            "migration_analysis": FeatureModelConfig(
                model="openrouter/google/gemini-2.0-flash",
                max_tokens=2000,
            ),
        },
    )
    client = ClaudeClient(config=config)
    model, max_tokens = client._resolve_feature("migration_analysis", default_max_tokens=1)
    assert model == "openrouter/google/gemini-2.0-flash"
    assert max_tokens == 2000


def test_resolve_feature_unknown_feature_falls_through_to_default_model():
    """A feature with no entry in either default map falls back to default_model."""
    from caretaker.llm.claude import ClaudeClient

    config = LLMConfig(
        provider="openrouter",
        default_model="openrouter/anthropic/claude-sonnet-4.6",
    )
    client = ClaudeClient(config=config)
    model, _ = client._resolve_feature("not_a_real_feature", default_max_tokens=1500)
    assert model == "openrouter/anthropic/claude-sonnet-4.6"
```

- [ ] **Step 3: Run the tests to verify they fail**

```bash
pytest tests/test_llm.py -k "resolve_feature" -v
```

Expected: at least three FAIL — `DEFAULT_FEATURE_MODELS_BY_PROVIDER` doesn't exist; the OpenRouter-provider path resolves to the legacy Claude model.

- [ ] **Step 4: Add `DEFAULT_FEATURE_MODELS_BY_PROVIDER` in `config.py`**

In `src/caretaker/config.py` immediately below the existing `DEFAULT_FEATURE_MODELS` (around line 315), add:

```python
# Provider-aware default feature models. Consulted by ClaudeClient._resolve_feature
# BEFORE the legacy DEFAULT_FEATURE_MODELS table when the provider matches.
# Operator's feature_models config still wins over both.
#
# Why per-provider rather than a single map: Anthropic operators don't want
# openrouter/-prefixed strings injected, and OpenRouter operators don't want
# bare 'claude-sonnet-4-5' strings (LiteLLM would route those Anthropic-direct,
# silently bypassing OpenRouter and breaking cost/billing/rate-limits).
DEFAULT_FEATURE_MODELS_BY_PROVIDER: dict[str, dict[str, dict[str, int | str]]] = {
    "openrouter": {
        "ci_log_analysis": {
            "model": "openrouter/deepseek/deepseek-r1",
            "max_tokens": 2000,
        },
        "principal_architecture_review": {
            "model": "openrouter/anthropic/claude-opus-4.6",
            "max_tokens": 4000,
        },
        "upgrade_impact_analysis": {
            "model": "openrouter/anthropic/claude-sonnet-4.6:online",
            "max_tokens": 3000,
        },
        "migration_analysis": {
            "model": "openrouter/anthropic/claude-sonnet-4.6:online",
            "max_tokens": 4000,
        },
        "migration_plan": {
            "model": "openrouter/anthropic/claude-sonnet-4.6:online",
            "max_tokens": 5000,
        },
        # Other features fall through to default_model.
    },
}
```

- [ ] **Step 5: Update `_resolve_feature` to consult the per-provider map first**

In `src/caretaker/llm/claude.py:138-155`, replace `_resolve_feature` with:

```python
def _resolve_feature(self, feature: str, default_max_tokens: int) -> tuple[str, int]:
    """Return the (model, max_tokens) pair for a feature.

    Resolution order:
      1. Operator's feature_models[feature] override.
      2. Caretaker's DEFAULT_FEATURE_MODELS_BY_PROVIDER[provider][feature].
      3. Legacy DEFAULT_FEATURE_MODELS[feature] (Anthropic-flavored defaults).
      4. config.default_model + the caller-supplied default_max_tokens.
    """
    if self._config is None:
        return _LEGACY_FEATURE_DEFAULTS.get(feature, (_FALLBACK_MODEL, default_max_tokens))

    from caretaker.config import (
        DEFAULT_FEATURE_MODELS,
        DEFAULT_FEATURE_MODELS_BY_PROVIDER,
    )

    # Provider-aware default first; fall back to legacy Anthropic-flavored map.
    by_provider = DEFAULT_FEATURE_MODELS_BY_PROVIDER.get(self._config.provider, {})
    base = by_provider.get(feature) or DEFAULT_FEATURE_MODELS.get(feature, {})
    override = self._config.feature_models.get(feature)

    model: str = str(base.get("model") or self._config.default_model)
    max_tokens = int(base.get("max_tokens") or default_max_tokens)
    if override is not None:
        if override.model:
            model = override.model
        if override.max_tokens:
            max_tokens = override.max_tokens
    return model, max_tokens
```

- [ ] **Step 6: Run the tests to verify they pass**

```bash
pytest tests/test_llm.py -k "resolve_feature" -v
```

Expected: PASS.

- [ ] **Step 7: Run full test_llm.py for regressions**

```bash
pytest tests/test_llm.py -v
```

Expected: all tests pass — Anthropic operators see no behavior change because their `provider="anthropic"` does not appear in `DEFAULT_FEATURE_MODELS_BY_PROVIDER`, so they fall through to `DEFAULT_FEATURE_MODELS` (unchanged path).

- [ ] **Step 8: Commit**

```bash
git add src/caretaker/config.py src/caretaker/llm/claude.py tests/test_llm.py
git commit -m "feat(llm): per-provider default feature models with OpenRouter map

Adds DEFAULT_FEATURE_MODELS_BY_PROVIDER consulted by _resolve_feature
before the legacy DEFAULT_FEATURE_MODELS table. Operator feature_models
overrides still win. Ships OpenRouter defaults: deepseek-r1 for CI logs,
claude-opus-4.6 for architectural review, claude-sonnet-4.6:online for
upgrade/migration analysis. Anthropic operators see no behavior change."
```

---

## Task 5: Strict prefix validation for OpenRouter configs

**Why this task matters:** Without this, an operator who sets `provider: openrouter` but forgets the `openrouter/` prefix on a model string gets routed Anthropic-direct via LiteLLM's bare-name fallback, silently bypassing OpenRouter. Their billing dashboard, rate limits, and observability all break in confusing ways. Failing fast at config-load with a clear message is the right trade.

**Files:**
- Modify: `src/caretaker/config.py` (add `model_validator` on `LLMConfig`)
- Test: `tests/test_config.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_config.py`:

```python
import pytest
from pydantic import ValidationError


def test_openrouter_provider_rejects_bare_default_model():
    """provider='openrouter' must reject default_model lacking the openrouter/ prefix."""
    from caretaker.config import LLMConfig

    with pytest.raises(ValidationError, match="openrouter/"):
        LLMConfig(provider="openrouter", default_model="claude-sonnet-4-5")


def test_openrouter_provider_rejects_bare_feature_model():
    """provider='openrouter' must reject feature_models entries lacking the prefix."""
    from caretaker.config import FeatureModelConfig, LLMConfig

    with pytest.raises(ValidationError, match="openrouter/"):
        LLMConfig(
            provider="openrouter",
            default_model="openrouter/anthropic/claude-sonnet-4.6",
            feature_models={
                "ci_log_analysis": FeatureModelConfig(model="claude-haiku-4-5"),
            },
        )


def test_openrouter_provider_rejects_bare_fallback_model():
    """provider='openrouter' must reject fallback_models entries lacking the prefix."""
    from caretaker.config import LLMConfig

    with pytest.raises(ValidationError, match="openrouter/"):
        LLMConfig(
            provider="openrouter",
            default_model="openrouter/anthropic/claude-sonnet-4.6",
            fallback_models=["claude-haiku-4-5"],
        )


def test_openrouter_provider_accepts_all_prefixed_models():
    """A fully-prefixed OpenRouter config validates cleanly."""
    from caretaker.config import FeatureModelConfig, LLMConfig

    config = LLMConfig(
        provider="openrouter",
        default_model="openrouter/anthropic/claude-sonnet-4.6",
        feature_models={
            "ci_log_analysis": FeatureModelConfig(model="openrouter/deepseek/deepseek-r1"),
        },
        fallback_models=["openrouter/google/gemini-2.0-flash"],
    )
    assert config.provider == "openrouter"


def test_anthropic_provider_still_accepts_bare_models():
    """Strict prefix check must NOT apply when provider != openrouter."""
    from caretaker.config import LLMConfig

    config = LLMConfig(provider="anthropic", default_model="claude-sonnet-4-5")
    assert config.default_model == "claude-sonnet-4-5"


def test_litellm_provider_still_accepts_mixed_prefixes():
    """provider='litellm' must accept any prefix shape; only openrouter is strict."""
    from caretaker.config import LLMConfig

    config = LLMConfig(
        provider="litellm",
        default_model="azure_ai/gpt-4o",
        fallback_models=["claude-sonnet-4-5", "openai/gpt-4o-mini"],
    )
    assert config.provider == "litellm"
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
pytest tests/test_config.py -k "openrouter or litellm_provider_still" -v
```

Expected: the four "rejects" tests FAIL (no validation happens yet); the two "accepts" tests PASS.

- [ ] **Step 3: Add the model validator**

In `src/caretaker/config.py`, find the existing imports and ensure `model_validator` is imported from pydantic alongside `field_validator`:

```python
from pydantic import AliasChoices, ConfigDict, Field, field_validator, model_validator
```

Then in `LLMConfig` (the class beginning at line 371), add an `@model_validator(mode="after")` method directly below the field declarations (i.e. after `bot_identity` at line 434):

```python
@model_validator(mode="after")
def _validate_openrouter_prefix(self) -> "LLMConfig":
    """When provider='openrouter', every model string must use the openrouter/ prefix.

    Prevents the silent bypass to Anthropic-direct that LiteLLM performs
    when given a bare 'claude-*' string — that bypass breaks billing,
    rate limits, and observability against the operator's intent.
    """
    if self.provider != "openrouter":
        return self

    bad: list[tuple[str, str]] = []
    if not self.default_model.startswith("openrouter/"):
        bad.append(("default_model", self.default_model))
    for feature, override in self.feature_models.items():
        if override.model and not override.model.startswith("openrouter/"):
            bad.append((f"feature_models.{feature}.model", override.model))
    for i, m in enumerate(self.fallback_models):
        if m and not m.startswith("openrouter/"):
            bad.append((f"fallback_models[{i}]", m))

    if bad:
        details = "; ".join(f"{path}={value!r}" for path, value in bad)
        raise ValueError(
            f"provider='openrouter' requires every model string to start with 'openrouter/'. "
            f"Offending entries: {details}. "
            f"Either prefix them (e.g. 'openrouter/anthropic/claude-sonnet-4.6') "
            f"or change provider to 'litellm' / 'anthropic'."
        )
    return self
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
pytest tests/test_config.py -k "openrouter or litellm_provider_still" -v
```

Expected: all six PASS.

- [ ] **Step 5: Run the full config test file for regressions**

```bash
pytest tests/test_config.py -v
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/caretaker/config.py tests/test_config.py
git commit -m "feat(config): strict openrouter/ prefix validation under provider=openrouter

Catches silent-bypass misconfigs at config-load time. When provider is
'openrouter', default_model, every feature_models[*].model, and every
fallback_models entry must start with 'openrouter/'. The error message
points at the offending entries with a fix suggestion."
```

---

## Task 6: `caretaker.llm.online=true` span attribute

**Why this task matters:** `:online` web search adds ~$4/1k searches on top of the model call. Operators need to see what fraction of LLM spend is web-grounded directly from the existing OTel telemetry without parsing model strings downstream. One attribute on the span lets cost dashboards filter on it.

**Files:**
- Modify: `src/caretaker/llm/provider.py` (in `LiteLLMProvider.complete` and `complete_with_tools`)
- Test: `tests/test_llm.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_llm.py`. Use the existing `_acompletion` mock pattern; if you're unsure of the exact shape, search the file for `mock_acompletion` or similar:

```python
@pytest.mark.asyncio
async def test_litellm_complete_sets_online_attribute_on_online_model(monkeypatch):
    """LiteLLMProvider tags the span with caretaker.llm.online=True for :online models."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-test")
    captured_attrs: dict[str, object] = {}

    class FakeSpan:
        def set_attribute(self, key: str, value: object) -> None:
            captured_attrs[key] = value
        def record_response(self, **kwargs):
            pass

    from contextlib import contextmanager
    @contextmanager
    def fake_span(**kwargs):
        # Capture extra_attrs which the wrapper splats onto the span.
        for k, v in (kwargs.get("extra_attrs") or {}).items():
            captured_attrs[k] = v
        yield FakeSpan()

    monkeypatch.setattr("caretaker.llm.provider.llm_chat_span", fake_span)

    async def fake_acompletion(**kwargs):
        class _Choice:
            def __init__(self):
                class _Msg: content = "ok"
                self.message = _Msg()
                self.finish_reason = "stop"
        class _Resp:
            choices = [_Choice()]
            usage = type("U", (), {"prompt_tokens": 1, "completion_tokens": 1})()
            id = "x"
            model = "openrouter/anthropic/claude-sonnet-4.6:online"
        return _Resp()

    p = LiteLLMProvider()
    if not p.package_installed:
        pytest.skip("litellm package not installed")
    p._acompletion = fake_acompletion  # type: ignore[assignment]

    await p.complete(LLMRequest(
        feature="upgrade_impact_analysis",
        prompt="hi",
        model="openrouter/anthropic/claude-sonnet-4.6:online",
        max_tokens=10,
    ))

    assert captured_attrs.get("caretaker.llm.online") is True


@pytest.mark.asyncio
async def test_litellm_complete_omits_online_attribute_on_non_online_model(monkeypatch):
    """No caretaker.llm.online attribute when the model lacks the :online suffix."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-test")
    captured_attrs: dict[str, object] = {}

    class FakeSpan:
        def set_attribute(self, key: str, value: object) -> None:
            captured_attrs[key] = value
        def record_response(self, **kwargs):
            pass

    from contextlib import contextmanager
    @contextmanager
    def fake_span(**kwargs):
        for k, v in (kwargs.get("extra_attrs") or {}).items():
            captured_attrs[k] = v
        yield FakeSpan()

    monkeypatch.setattr("caretaker.llm.provider.llm_chat_span", fake_span)

    async def fake_acompletion(**kwargs):
        class _Choice:
            def __init__(self):
                class _Msg: content = "ok"
                self.message = _Msg()
                self.finish_reason = "stop"
        class _Resp:
            choices = [_Choice()]
            usage = type("U", (), {"prompt_tokens": 1, "completion_tokens": 1})()
            id = "x"
            model = "openrouter/anthropic/claude-sonnet-4.6"
        return _Resp()

    p = LiteLLMProvider()
    if not p.package_installed:
        pytest.skip("litellm package not installed")
    p._acompletion = fake_acompletion  # type: ignore[assignment]

    await p.complete(LLMRequest(
        feature="ci_log_analysis",
        prompt="hi",
        model="openrouter/anthropic/claude-sonnet-4.6",
        max_tokens=10,
    ))

    assert "caretaker.llm.online" not in captured_attrs
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
pytest tests/test_llm.py -k "online_attribute" -v
```

Expected: the first test FAILs (`caretaker.llm.online` is not yet set anywhere); the second PASSes (vacuous — attribute is absent).

- [ ] **Step 3: Set the span attribute in `LiteLLMProvider.complete`**

In `src/caretaker/llm/provider.py` inside `LiteLLMProvider.complete` (around line 356), modify the `extra_attrs` dict passed to `llm_chat_span`:

```python
with llm_chat_span(
    system=_genai_system_for_model(request.model),
    model=request.model,
    max_tokens=request.max_tokens,
    temperature=request.temperature,
    extra_attrs={
        "caretaker.llm.feature": request.feature,
        "caretaker.llm.router": "litellm",
        **({"caretaker.llm.online": True} if request.model.endswith(":online") else {}),
    },
) as span:
```

And do the same in `complete_with_tools` (around line 447):

```python
with llm_chat_span(
    system=_genai_system_for_model(request.model),
    model=request.model,
    operation="chat.tool_use",
    max_tokens=request.max_tokens,
    temperature=request.temperature,
    extra_attrs={
        "caretaker.llm.feature": request.feature,
        "caretaker.llm.router": "litellm",
        "caretaker.llm.tool_count": len(tools),
        **({"caretaker.llm.online": True} if request.model.endswith(":online") else {}),
    },
) as _tool_span:
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
pytest tests/test_llm.py -k "online_attribute" -v
```

Expected: both PASS.

- [ ] **Step 5: Run full test_llm.py for regressions**

```bash
pytest tests/test_llm.py -v
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/caretaker/llm/provider.py tests/test_llm.py
git commit -m "feat(observability): tag :online model spans with caretaker.llm.online

Sets caretaker.llm.online=True on the GenAI span when the resolved
model string ends with :online. Cost dashboards can now filter on it
to break out web-grounded LLM spend without parsing model strings."
```

---

## Task 7: Doctor recognizes `openrouter/` prefix

**Why this task matters:** During plan-writing we discovered that `caretaker.doctor` already has a sophisticated `_MODEL_PREFIX_ENV_MAP` that walks every distinct model string in the config and reports missing env vars. This is much cleaner than the spec's originally proposed standalone "doctor check" — adding one tuple entry covers `default_model`, `feature_models`, and `fallback_models` simultaneously, with proper FAIL/WARN severity (FAIL when it's a primary model, WARN when only in the fallback chain).

**Files:**
- Modify: `src/caretaker/doctor.py:381-395` (`_MODEL_PREFIX_ENV_MAP`)
- Test: `tests/test_doctor_llm_env.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_doctor_llm_env.py` (follow the existing test patterns in that file — look at how other prefix→env tests are structured):

```python
def test_doctor_reports_missing_openrouter_key_for_primary_model(monkeypatch):
    """When default_model is openrouter/* and OPENROUTER_API_KEY is missing → FAIL row."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    from caretaker.config import LLMConfig, MaintainerConfig
    from caretaker.doctor import _collect_llm_env_references, check_env_secrets

    # Use whatever existing fixture or factory the file uses to build a MaintainerConfig.
    # Adjust this construction to match the patterns already used in this test file.
    config = _make_minimal_maintainer_config(
        llm=LLMConfig(
            provider="openrouter",
            default_model="openrouter/anthropic/claude-sonnet-4.6",
        )
    )

    refs = _collect_llm_env_references(config)
    env_names = {ref.env_name for ref in refs}
    assert "OPENROUTER_API_KEY" in env_names

    # Severity check: primary model with missing env → FAIL.
    results = check_env_secrets(config)
    openrouter_results = [r for r in results if "OPENROUTER_API_KEY" in (r.detail or "") or "OPENROUTER_API_KEY" in r.name]
    assert openrouter_results, "expected a row referencing OPENROUTER_API_KEY"
    assert any(r.severity.name == "FAIL" for r in openrouter_results)


def test_doctor_recognizes_openrouter_prefix_in_fallback_only(monkeypatch):
    """An openrouter/* model that appears only in fallback_models → WARN, not FAIL."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    from caretaker.config import LLMConfig, MaintainerConfig
    from caretaker.doctor import check_env_secrets

    config = _make_minimal_maintainer_config(
        llm=LLMConfig(
            provider="litellm",  # litellm allows mixed prefixes
            default_model="anthropic/claude-sonnet-4.6",
            fallback_models=["openrouter/anthropic/claude-sonnet-4.6"],
        )
    )

    results = check_env_secrets(config)
    openrouter_rows = [r for r in results if "OPENROUTER_API_KEY" in (r.detail or "") or "OPENROUTER_API_KEY" in r.name]
    assert openrouter_rows, "expected a row referencing OPENROUTER_API_KEY"
    # Fallback-only → owner_enabled=False → WARN, not FAIL.
    assert all(r.severity.name == "WARN" for r in openrouter_rows)
```

If `tests/test_doctor_llm_env.py` does not have a `_make_minimal_maintainer_config` helper, look for the existing patterns it uses to build `MaintainerConfig` objects (likely a fixture or inline constructor) and copy that idiom verbatim.

- [ ] **Step 2: Run the tests to verify they fail**

```bash
pytest tests/test_doctor_llm_env.py -k "openrouter" -v
```

Expected: both FAIL — `_env_vars_for_model("openrouter/...")` returns `((), None)` because no prefix matches, so the doctor never emits a row referencing `OPENROUTER_API_KEY`.

- [ ] **Step 3: Add the openrouter prefix to the env map**

In `src/caretaker/doctor.py:381-395`, add a new tuple entry to `_MODEL_PREFIX_ENV_MAP`. Place it at the top (longest/most specific first, per the existing comment):

```python
_MODEL_PREFIX_ENV_MAP: tuple[tuple[str, tuple[str, ...]], ...] = (
    # Longest / most specific first.
    ("openrouter/", ("OPENROUTER_API_KEY",)),
    ("ollama_chat/", ("OLLAMA_API_BASE",)),
    ("vertex_ai/", ("GOOGLE_APPLICATION_CREDENTIALS", "VERTEX_PROJECT")),
    ("azure_ai/", ("AZURE_AI_API_KEY", "AZURE_AI_API_BASE")),
    ("anthropic/", ("ANTHROPIC_API_KEY",)),
    ("bedrock/", ("AWS_ACCESS_KEY_ID",)),
    ("openai/", ("OPENAI_API_KEY",)),
    ("ollama/", ("OLLAMA_API_BASE",)),
    ("mistral/", ("MISTRAL_API_KEY",)),
    ("cohere/", ("COHERE_API_KEY",)),
    ("gemini/", ("GEMINI_API_KEY",)),
    ("groq/", ("GROQ_API_KEY",)),
    ("azure/", ("AZURE_API_KEY", "AZURE_API_BASE")),
)
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
pytest tests/test_doctor_llm_env.py -k "openrouter" -v
```

Expected: PASS.

- [ ] **Step 5: Run full doctor test files for regressions**

```bash
pytest tests/test_doctor.py tests/test_doctor_llm_env.py tests/test_doctor_llm_probe.py -v
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/caretaker/doctor.py tests/test_doctor_llm_env.py
git commit -m "feat(doctor): recognize openrouter/ prefix in env-var preflight check

Adds OPENROUTER_API_KEY to the model-prefix → env-vars map so caretaker
doctor reports missing OpenRouter credentials with the same FAIL/WARN
severity rules as every other provider. No new check function needed."
```

---

## Task 8: Documentation

**Why this task matters:** Without docs the integration is undiscoverable. This task adds a top-level README section pointing operators at OpenRouter and a configuration.md section with the full schema, including the explicit `:online` cost call-out the spec requires.

**Files:**
- Modify: `README.md` (add a section sibling to "Optional: Claude Integration")
- Modify: `docs/configuration.md` (extend the LLM config reference)

This task has no automated tests — it's documentation. The verification step is reading the rendered docs.

- [ ] **Step 1: Add the OpenRouter section to README.md**

Find the existing "Optional: Claude Integration" section in `README.md` (around line 185-193). Add a sibling section directly below it:

```markdown
### Optional: OpenRouter Integration

Add `OPENROUTER_API_KEY` and set `provider: openrouter` in
`.github/maintainer/config.yml` to route LLM calls through
[OpenRouter](https://openrouter.ai), which gives you:

- **300+ models behind one key** — DeepSeek R1, Gemini, Llama, Qwen,
  GLM, plus all the proprietary frontier models.
- **Per-feature model routing** — pin different caretaker features to
  different best-fit models via `feature_models`.
- **Web-grounded analysis** — append `:online` to a model string to add
  a web search step before the completion. Caretaker ships this as the
  default for `upgrade_impact_analysis`, `migration_analysis`, and
  `migration_plan` so release-note and breaking-change context comes
  from current sources rather than stale model knowledge.

Sample config:

\`\`\`yaml
llm:
  provider: openrouter
  default_model: openrouter/anthropic/claude-sonnet-4.6
  feature_models:
    ci_log_analysis:
      model: openrouter/deepseek/deepseek-r1
    architectural_review:
      model: openrouter/anthropic/claude-opus-4.6
\`\`\`

**Cost note:** `:online` adds OpenRouter's web-search step
(~$4 per 1k searches) on top of the model call. The
`caretaker.llm.online=true` OTel span attribute lets you break out
web-grounded spend in cost dashboards.

When `provider: openrouter` is set, every model string must begin
with `openrouter/`. Caretaker rejects bare model names at
config-load to prevent the silent bypass to Anthropic-direct that
LiteLLM otherwise performs.
```

(Replace the escaped backticks `\`\`\`` with literal triple backticks when writing the file.)

- [ ] **Step 2: Extend `docs/configuration.md`**

Open `docs/configuration.md` and find the existing LLM configuration section. Add a subsection covering:

1. The full set of valid `provider` values: `anthropic`, `litellm`, `openrouter`.
2. The `feature_models` schema with an OpenRouter example.
3. The strict-prefix rule for `provider: openrouter` and the error operators see when they violate it.
4. A pointer to the model catalog at https://openrouter.ai/models.
5. The `:online` cost note (mirrored from README).
6. Pre-deployment note: the `OPENROUTER_API_KEY` must be available in the runtime environment (e.g. as a SealedSecret in the bigboy k8s manifests for Azure deployments).

Match the style of the surrounding sections in `docs/configuration.md` — likely yaml fenced blocks with prose between.

- [ ] **Step 3: Render and inspect the docs**

```bash
mkdocs serve
```

Open the local site, navigate to the configuration page, and confirm:

- The new section renders cleanly (no broken yaml fences, no missing headings).
- Cross-link to README OpenRouter section works.
- Code blocks have correct language tags (`yaml`, not `yml`, to match site convention).

- [ ] **Step 4: Commit**

```bash
git add README.md docs/configuration.md
git commit -m "docs: add OpenRouter integration section to README and config reference

Documents provider=openrouter, per-feature model routing via
feature_models, the strict openrouter/ prefix rule, the :online
suffix and its web-search cost surcharge, and the OTel attribute
for breaking out web-grounded spend."
```

---

## Final verification

- [ ] **Step 1: Run the full test suite**

```bash
pytest tests/ -v
```

Expected: all tests pass.

- [ ] **Step 2: Run the type checker**

```bash
mypy src/
```

Expected: no new errors. (Pre-existing baseline errors recorded in `mypy.log` are acceptable; the diff vs. that baseline must be empty.)

- [ ] **Step 3: Run the linter**

```bash
ruff check src/ tests/
ruff format --check src/ tests/
```

Expected: clean.

- [ ] **Step 4: Manual smoke test (operator perspective)**

In a scratch directory, create a minimal `config.yml`:

```yaml
llm:
  provider: openrouter
  default_model: openrouter/anthropic/claude-sonnet-4.6
  feature_models:
    ci_log_analysis:
      model: openrouter/deepseek/deepseek-r1
```

Set `OPENROUTER_API_KEY` in your shell from the value in `~/.zshrc`. Run:

```bash
caretaker doctor --config /path/to/config.yml
```

Expected output: an OpenRouter row reporting `OPENROUTER_API_KEY` as `OK`. The two distinct models (`openrouter/anthropic/claude-sonnet-4.6` and `openrouter/deepseek/deepseek-r1`) should each appear in the `--llm-probe` output if you pass that flag, both routed to the `OPENROUTER_API_KEY` env requirement.

- [ ] **Step 5: Pre-merge deployment checklist (operational, not code)**

Per the spec: caretaker's runtime k8s manifests live in the **bigboy** project, not this repo. Do these steps **before** merging the caretaker PR, otherwise the deployed pods will crash on missing env on the next rollout:

1. In `~/Projects/bigboy`, take the `OPENROUTER_API_KEY` value from `~/.zshrc` and `kubeseal` it into a `SealedSecret` against the Azure cluster's controller cert.
2. Wire the new secret into the caretaker `Deployment` env block and any agent-task `Job` template that reads it.
3. Open and merge the bigboy PR FIRST.
4. Then merge the caretaker PR.

---

## Plan self-review

Before handing off to execution:

- **Spec coverage:** Spec Change 1 → Task 1. Change 2 → Tasks 2 + 3. Change 3 → Task 4. Change 4 → Task 5. Change 5 → Task 6. Change 6 → Task 7 (simpler than spec described — one tuple entry to the existing `_MODEL_PREFIX_ENV_MAP`, not a new check function. Worth noting in the spec post-merge). Change 7 → Task 8. ✓
- **No placeholders:** Every step has either an exact command, exact file/line reference, or full code body. The Task 8 README block uses escaped backticks because the plan itself is a markdown file — explicit instruction to unescape on write. ✓
- **Type consistency:** `_resolve_feature` signature and return shape (`tuple[str, int]`) is preserved across Tasks 4 and the existing call sites. `DEFAULT_FEATURE_MODELS_BY_PROVIDER` annotation matches the existing `DEFAULT_FEATURE_MODELS` shape. The `model_validator` in Task 5 uses pydantic v2 syntax matching the existing `field_validator` usage in `ModelPoolConfig`. ✓
- **TDD discipline:** Every code-bearing task starts with a failing test. ✓

---

## Execution handoff

Plan complete and saved to [`docs/superpowers/plans/2026-05-01-openrouter-integration.md`](2026-05-01-openrouter-integration.md). Two execution options:

1. **Subagent-Driven (recommended)** — Dispatch a fresh subagent per task, review between tasks, fast iteration.
2. **Inline Execution** — Execute tasks in this session using `executing-plans`, batch execution with checkpoints.

Which approach?
