# QA Scenario 11 — Azure AI Foundry + Anthropic Prompt Cache

**Date:** 2026-04-22
**Scenario issue:** https://github.com/ianlintner/caretaker-qa/issues/21
**Config overlay PR:** https://github.com/ianlintner/caretaker-qa/pull/22

## What the scenario validates

That caretaker, when routed through Azure AI Foundry to a Claude-family deployment (`azure_ai/claude-sonnet-4`), still:

1. Injects Anthropic `cache_control` markers on eligible prompts — driven by `_supports_prompt_cache` in `src/caretaker/llm/provider.py:45-56`, which substring-matches `"claude"` on the model id.
2. Receives a real prompt-cache hit from the upstream Claude endpoint when the same prompt bytes are re-sent.
3. Records that hit in the `caretaker_llm_cache_read_tokens_total` Counter (`src/caretaker/observability/metrics.py:284`) with the correct `provider=litellm,model=azure_ai/claude-sonnet-4` label set — i.e. no silent drift to `provider=anthropic` or loss of the `azure_ai/` prefix.

The `ci_log_analysis` feature is chosen as the target because (a) it is already in `CLAUDE_FEATURES` (`src/caretaker/llm/router.py:17-32`) so `feature_enabled` is True with no extra config, (b) its prompts are small and highly repeatable — the scenario-11 issue body carries a deterministic ~800-token synthetic CI-failure excerpt that is re-consumed on every caretaker-qa run, which is the prompt-cache's best case.

## Why it matters

Azure AI Foundry is now the production Claude path for the whole caretaker fleet: direct Anthropic API keys are being deprecated in favor of Azure-issued credentials, and every consumer repo (including this one) is expected to route Claude through `litellm → azure_ai/*`. The caching integration between Anthropic's `cache_control` protocol and the Azure AI Foundry passthrough is currently **implicit**: `provider.py` adds the marker based on a string match, and we rely on LiteLLM + Azure AI Foundry to forward it without mangling. No unit test, no integration test, and no existing QA scenario locks this in.

That means any of the following refactors would silently break prompt-cache economics for the entire fleet without a single CI signal firing:

- Tightening `_CACHE_CAPABLE_MODEL_MARKERS` to something stricter than substring `"claude"` (e.g. requiring a leading `anthropic/` or `claude-` prefix) — this would exclude `azure_ai/claude-sonnet-4` and we'd pay cache-miss prices on every run.
- A LiteLLM version bump that changes how Azure AI routes `cache_control` content blocks.
- Azure AI Foundry itself silently stripping the cache-control headers at its proxy.
- A caretaker refactor that records cache usage under a different label set (dropping the `azure_ai/` prefix, or setting `provider=anthropic` because the *underlying* model vendor is Anthropic).

Scenario 11 exists to catch all four from a single, cheap, daily-running signal.

## Counters and labels to assert on

| Counter | Labels (required) | Expected behavior |
|---------|-------------------|-------------------|
| `caretaker_llm_cache_creation_tokens_total` | `provider=litellm`, `model=azure_ai/claude-sonnet-4` | Increments on run 1 (cold cache); flat on run 2+ |
| `caretaker_llm_cache_read_tokens_total` | `provider=litellm`, `model=azure_ai/claude-sonnet-4` | 0 on run 1; increments on run 2+ |

Specifically:

- `provider` label **must** be `litellm` — not `anthropic`. caretaker-qa has `claude_enabled: "false"` and no `ANTHROPIC_API_KEY`, so a value of `anthropic` would indicate a wrongly-configured fallback path.
- `model` label **must** retain the `azure_ai/` prefix. Stripping it silently would still work for Anthropic billing but would break every Grafana dashboard that groups cache-hit ratio by provider.
- `feature_enabled("ci_log_analysis")` must remain True (the feature is in `CLAUDE_FEATURES`; this is covered by existing unit tests, but the scenario pins the contract end-to-end).

## Follow-ups

- **Memory-snapshot upload bug:** scenario 11 is currently only partially self-verifying because the `.caretaker-memory-snapshot.json` artifact upload step fails on dotfile globbing (the `include-hidden-files: true` gap surfaced in the recent QA review — same bug that has been eating snapshot output from `flashcards` and friends). Until that is fixed, metric assertions must be made by scraping workflow logs. Once fixed, scenario 11 becomes a pure post-run diff against the snapshot JSON and can be folded into a scheduled nightly-eval assertion.
- **Promote to enforce:** today `ci_log_analysis` runs in `shadow` mode alongside the rest of the agentic-migration work. Once the cache-hit ratio stabilizes above a threshold (target: ≥ 0.6 over a 7-day window) we should move it out of shadow.
- **Fleet rollout:** once scenario 11 is green on caretaker-qa for ≥ 1 week, replicate the `feature_models.ci_log_analysis.model = azure_ai/claude-sonnet-4` pin into the other consumer repos (`python_dsa`, `kubernetes-apply-vscode`, `Example-React-AI-Chat-App`) so their CI-failure triage shares the same Claude + Azure AI routing and benefits from the same prompt-cache economics.
- **Eventual deletion of the Anthropic-direct path:** when the whole fleet is on `azure_ai/claude-*` and scenario 11 has held for a release cycle, we can drop the `anthropic/*` branch in `_supports_prompt_cache` and simplify the provider module.

## Related

- `src/caretaker/llm/provider.py:40-56` — `_CACHE_CAPABLE_MODEL_MARKERS` and `_supports_prompt_cache`.
- `src/caretaker/llm/router.py:17-32` — `CLAUDE_FEATURES` set.
- `src/caretaker/observability/metrics.py:270-296` — prompt-cache Counter definitions.
- PR #479 (caretaker) — introduced the cache-token counters this scenario pins down.
