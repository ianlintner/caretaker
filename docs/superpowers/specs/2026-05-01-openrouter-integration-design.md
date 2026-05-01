# OpenRouter Integration — Design

**Date:** 2026-05-01
**Status:** Approved (brainstorm), pending implementation plan
**Scope:** Per-feature model routing (A) + web-grounded analysis via `:online` (C)

---

## Motivation

Caretaker today routes all LLM analysis features (CI log triage, architectural
review, upgrade impact, migration planning, etc.) through a single configured
provider — typically Anthropic-direct or LiteLLM with one model. Two limitations
follow:

1. **One model fits all features.** A cheap Gemini Flash call would suffice for
   label triage; an architectural review wants Claude Opus or DeepSeek R1.
   Today everything pays the same per-token rate as the highest-capability
   feature.
2. **No web-grounded context.** Features that benefit from current external
   information — `upgrade_impact_analysis` (release notes), `migration_analysis`
   / `migration_plan` (breaking-change advisories) — answer from stale model
   knowledge.

OpenRouter solves both behind a single API key: 300+ models from one credential,
plus the `:online` model-string suffix that adds a web search step before the
completion.

The codebase already has the structural hooks needed: `LLMConfig.feature_models`
and `_resolve_feature()` (`src/caretaker/llm/claude.py:138`) implement
per-feature model resolution today. `LiteLLMProvider`
(`src/caretaker/llm/provider.py:275`) already accepts `openrouter/...` model
strings. The integration is therefore additive and small.

---

## Scope

### In scope

- New recognized provider name `openrouter` that constructs a `LiteLLMProvider`
  configured for OpenRouter credentials.
- Caretaker-shipped per-feature model defaults specifically for the OpenRouter
  provider, including `:online` suffix on three features: `upgrade_impact_analysis`,
  `migration_analysis`, `migration_plan`.
- Operator-overridable per-feature model map via existing `feature_models`
  config — unchanged, just exercised through the new defaults.
- Strict config validation: when `provider: openrouter`, model strings must be
  prefixed with `openrouter/` to prevent silent bypass to Anthropic-direct.
- Telemetry tagging: spans flag `caretaker.llm.online=true` when the resolved
  model carries the `:online` suffix, so cost dashboards can break out web-
  grounded spend.
- Doctor (`caretaker.doctor`) warning when `provider: openrouter` is set but
  `OPENROUTER_API_KEY` is missing.
- Documentation: README sibling section to "Optional: Claude Integration" with
  explicit `:online` cost call-out.

### Out of scope (future iterations)

- Cost guardrails / hard spend caps.
- Cross-provider fallback chains via OpenRouter's `models` array.
- `:floor` (cheapest underlying provider) and `:nitro` (fastest) variants.
- BYOK passthrough (caretaker uses direct keys).
- A/B shadow comparison of two models for the same prompt.
- `openrouter/auto` autorouter (gives up the per-feature control this design
  is built around).

---

## Architecture

Three surgical changes; no new files, no new abstractions.

### Change 1 — `LiteLLMProvider.available` recognizes OpenRouter

**File:** `src/caretaker/llm/provider.py:317-330`

Add `"OPENROUTER_API_KEY"` to the env-var allowlist that
`LiteLLMProvider.available` consults. Without this, an operator who configures
only `OPENROUTER_API_KEY` gets a `NullProvider` even though LiteLLM itself
would route the request fine. This is also a latent bug for any user wanting
LiteLLM+OpenRouter today; the fix is independent of the rest of the design.

### Change 2 — `provider: openrouter` alias in the factory

**File:** `src/caretaker/llm/provider.py:701-724` (`build_provider`)

Recognize `"openrouter"` as a provider name. Constructs a `LiteLLMProvider`
with one extra behavior: log a clear warning when `OPENROUTER_API_KEY` is
missing, rather than the generic "no credentials" message. No new class.

```python
if name == "openrouter":
    if not os.environ.get("OPENROUTER_API_KEY"):
        logger.warning(
            "provider='openrouter' but OPENROUTER_API_KEY is not set"
        )
    return LiteLLMProvider(
        fallback_models=fallback_models,
        timeout=timeout,
    )
```

### Change 3 — Provider-aware default feature models

**File:** `src/caretaker/config.py` (alongside `DEFAULT_FEATURE_MODELS`)

Today `DEFAULT_FEATURE_MODELS` is a single dict of Claude model strings.
Extend with a per-provider lookup that the resolver consults first:

```python
DEFAULT_FEATURE_MODELS_BY_PROVIDER: dict[str, dict[str, dict[str, Any]]] = {
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
        # other features fall through to default_model
    },
}
```

`_resolve_feature` in `src/caretaker/llm/claude.py:138` consults
`DEFAULT_FEATURE_MODELS_BY_PROVIDER[config.provider]` first, falling back to
the existing `DEFAULT_FEATURE_MODELS` (which becomes the implicit Anthropic-
default table). Operator's `feature_models` override still wins — that
precedence rule is unchanged.

**Why per-provider rather than a single map:** Anthropic operators don't want
`openrouter/...` model strings injected, and OpenRouter operators don't want
bare `claude-sonnet-4-5` strings (which LiteLLM would route Anthropic-direct,
silently bypassing OpenRouter and breaking cost/billing/rate-limits in
confusing ways).

### Change 4 — Strict config validation

**File:** `src/caretaker/config.py` (`LLMConfig` post-init or load-time check)

When `provider == "openrouter"`, every model string in scope must start with
`openrouter/`. Validate:

- `default_model`
- Every `feature_models[*].model` value
- Every entry in `fallback_models`

Raise a config error at startup with a message that suggests the prefix. This
catches the most common misconfig — copy-pasting an Anthropic config into a
caretaker setup that intends to use OpenRouter — at config-load time rather
than as a confusing routing bug at run time.

### Change 5 — Online-suffix telemetry

**File:** `src/caretaker/observability/llm_span.py` (or where spans are built)

When the resolved model string ends with `:online`, set
`caretaker.llm.online=true` on the span. Implementation is one line at request
build time. This makes "what fraction of our LLM spend is web-grounded"
answerable directly from telemetry without parsing model strings downstream.

### Change 6 — Doctor check

**File:** `src/caretaker/doctor.py`

Add a check: if `LLMConfig.provider == "openrouter"` and `OPENROUTER_API_KEY`
is unset, emit a warning (not an error). Mirrors the existing Anthropic check.

### Change 7 — Documentation

**File:** `README.md` (sibling section to "Optional: Claude Integration")
**File:** `docs/configuration.md` (full config schema reference)

Add:

- Sample config block showing `provider: openrouter` with `feature_models`.
- Explicit note that `:online` carries an additional ~$4 / 1k searches cost
  on top of the model call.
- Pointer to OpenRouter model catalogue and how to choose models.

---

## Configuration surface

Operator-facing config in `.github/maintainer/config.yml`:

```yaml
llm:
  provider: openrouter
  default_model: openrouter/anthropic/claude-sonnet-4
  feature_models:
    ci_log_analysis:
      model: openrouter/deepseek/deepseek-r1
      max_tokens: 2500
    architectural_review:
      model: openrouter/anthropic/claude-opus-4
    upgrade_impact_analysis:
      model: openrouter/anthropic/claude-sonnet-4:online
```

Feature → model resolution order (already implemented; just extended):

1. Operator's `feature_models[feature]` — wins if present.
2. Caretaker's `DEFAULT_FEATURE_MODELS_BY_PROVIDER[provider][feature]` —
   provider-aware shipped default.
3. Operator's `default_model` — fallback for any feature without a specific
   mapping.

The shipped `:online` defaults (Section: Architecture / Change 3) are config
defaults, not enforced in code. An operator who sets
`feature_models.migration_analysis.model: openrouter/anthropic/claude-sonnet-4`
(no `:online`) gets exactly that — caretaker does not re-append the suffix.

---

## Backwards compatibility

This is purely additive:

- Operators on `provider: anthropic` see no behavior change. Their resolution
  falls through to the unchanged `DEFAULT_FEATURE_MODELS` table.
- Operators on `provider: litellm` with non-OpenRouter models see no behavior
  change.
- Operators on `provider: litellm` with `OPENROUTER_API_KEY` and bare
  `openrouter/...` model strings now work where they didn't before (Change 1
  is a bug fix).

No migration guide required. No config schema breakage.

---

## Testing

All tests are unit-level; no live OpenRouter calls. LiteLLM's `acompletion`
is already mocked in the existing test fixtures.

- `build_provider("openrouter")` returns a `LiteLLMProvider`.
- `build_provider("openrouter")` logs the warning when `OPENROUTER_API_KEY`
  is unset; logs nothing extra when it is set.
- `LiteLLMProvider.available` returns `True` when only `OPENROUTER_API_KEY`
  is set (regression for Change 1).
- `_resolve_feature("migration_analysis")` with `provider="openrouter"` and no
  operator override returns
  `("openrouter/anthropic/claude-sonnet-4:online", 4000)`.
- `_resolve_feature("migration_analysis")` with `provider="anthropic"` returns
  the legacy Claude default (unchanged behavior).
- `_resolve_feature("migration_analysis")` with operator override
  `feature_models.migration_analysis.model = openrouter/google/gemini-2.0-flash`
  returns the override; the `:online` default does not get re-applied.
- `LLMConfig` raises a config error when `provider="openrouter"` and
  `default_model="claude-sonnet-4-5"` (no prefix).
- `LLMConfig` raises a config error when `provider="openrouter"` and any
  `feature_models[*].model` lacks the `openrouter/` prefix.
- `LLMConfig` raises a config error when `provider="openrouter"` and any
  `fallback_models` entry lacks the `openrouter/` prefix.
- Span attribute `caretaker.llm.online=true` is set when resolved model ends
  with `:online`; absent or `false` otherwise.
- Doctor warns (does not error) when `provider="openrouter"` and
  `OPENROUTER_API_KEY` is unset.

---

## Deployment / rollout (operational, not code)

Caretaker's runtime k8s manifests live in the **bigboy** project, not this
repository. The `OPENROUTER_API_KEY` must reach Azure pods via a SealedSecret
before any merge of code that consumes it.

Pre-merge checklist:

1. Open caretaker PR with the seven changes + tests + docs.
2. In `bigboy`, create a `SealedSecret` for `OPENROUTER_API_KEY` (`kubeseal`
   the value from `$OPENROUTER_API_KEY` in `~/.zshrc` against the Azure
   cluster controller cert).
3. Wire the secret into the caretaker `Deployment` env and any agent-task
   `Job` template that reads it.
4. **Merge bigboy first, then caretaker.** Reverse order causes pods to crash
   on missing env on the next deployment.

---

## Open questions

None at design time. All decisions resolved during brainstorming:

- Per-feature model routing approach: **direct feature → model map** (not
  tiered).
- `:online` features: `upgrade_impact_analysis`, `migration_analysis`,
  `migration_plan`.
- `:online` enforcement: **shipped as config default, not enforced in code.**
- Bare-model-string handling under `provider: openrouter`: **strict validation,
  raise at config load.**

---

## Risks & mitigations

| Risk | Mitigation |
| ---- | ---------- |
| `:online` web search costs surprise operators | Explicit cost note in README; `caretaker.llm.online=true` span attribute for cost-dashboard breakdown. |
| OpenRouter outage takes down all caretaker LLM calls | Out of scope for this iteration; future work covers fallback chains. Today's behavior matches current single-provider risk. |
| LiteLLM `completion_cost` may underreport OpenRouter costs | Existing telemetry path; same risk that exists for any LiteLLM-routed model. Not regressed. |
| Operator copies an Anthropic config and sets `provider: openrouter` | Strict validation (Change 4) catches bare model strings at config load, with a message suggesting the prefix. |
| bigboy SealedSecret step forgotten before merge | Pre-merge checklist in this spec; project memory entry pinned for future sessions. |
