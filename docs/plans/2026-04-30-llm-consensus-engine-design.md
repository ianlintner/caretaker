# LLM Consensus Engine — Design (2026-04-30)

## Status

**Draft for review.** This document specifies the architecture; the implementation
plan is a follow-up artefact (see "Out of scope" below). Once this design is
approved on PR, the `writing-plans` skill produces the executable plan and
implementation lands in a separate PR.

## Problem

Caretaker's decision layer is a mix of heuristics and single-LLM-call sites.
The 2026-Q2 agentic migration ([2026-Q2-agentic-migration.md](2026-Q2-agentic-migration.md))
moved ten decision sites onto `@shadow_decision` so a legacy heuristic and an
LLM candidate run side-by-side, with operators flipping `enforce` mode per site
once disagreement data justified it. That infrastructure is working — but it
has two limits we now need to push past:

1. **Single-model brittleness on irreversible decisions.** `readiness` (the merge
   gate) and `dispatch_guard` (deciding what runs) both rely on one model's
   self-confidence. A confidently wrong verdict on `readiness` ships a bad
   merge; today there is no second opinion.
2. **Pure-heuristic gates with known failure modes.** `foundry/size_classifier.py`
   decides Foundry-vs-Copilot routing on file count and total line count alone.
   A 200-line config bump and a 200-line refactor route identically; the gate
   has no way to express "this is mechanical" vs "this is genuinely complex."

Separately, the LLM landscape is moving fast: a model that's the right primary
today may be deprecated or superseded by a better or cheaper option in 6-12
months. The current per-site `model_override` in `AgenticDomainConfig` pins
literal model strings, which means every model swap is a config edit on every
site.

## Goal

Add a **tiered-consensus engine** that lets decision sites consult one or more
models — selected from a capability-tagged provider pool — and ship the LLM
verdict as the authoritative decision. Land this on two proof-point sites:

- `readiness` (judgment replacement, irreversible action) — opt into a
  two-model agreement strategy on day one.
- `foundry/size_classifier` (heuristic augmentation) — keep the deterministic
  count gates as fast pre/post filters; consult the engine only on borderline
  cases that the count gate cannot judge well.

This PR makes the LLM/consensus path **authoritative** for both sites. The
existing `@shadow_decision` decorator stays in place but its role narrows: it
becomes a fallback-orchestrating wrapper that runs the legacy heuristic only
when the engine is unavailable, so the orchestrator never wedges on a provider
outage.

## Non-goals

- Migrating the other eight `@shadow_decision` sites onto the engine. Those can
  opt in incrementally as the engine proves itself.
- Threshold auto-learning from shadow disagreement records. Operators set
  thresholds manually until shadow data justifies a learner.
- A standalone `@consensus_decision` decorator separate from `@shadow_decision`.
  Both decorators wrapping the same site is on the maturity roadmap, not in
  this PR.
- Deleting the legacy `pr_agent/states.py` 10/20/30/40 readiness rubric. It
  stays as the conservative fallback for one release; deletion is a follow-up
  once `readiness` has run on `enforce` mode without surprises for ~2 weeks.
- Eval/Braintrust integration of consensus traces. Stubbed but not landed in
  this PR.

## Approach

### Architecture overview

A new `caretaker/consensus/` module exposes a `ConsensusEngine` instantiated
once at orchestrator startup. The engine accepts a per-call request specifying
the site name, the response schema, the prompt parts, and the feature gate.
It resolves the configured strategy and provider tags, runs the strategy, and
returns the verdict alongside a `ConsensusTrace` audit record.

```
src/caretaker/consensus/
├── __init__.py        # Public surface: ConsensusEngine, ConsensusResult,
│                      # ConsensusUnavailable, ConsensusTrace
├── engine.py          # ConsensusEngine.decide(...) — the public API
├── strategies.py      # tiered_confidence + always_two_models implementations
├── provider_pool.py   # Capability-tag → concrete model resolution
└── trace.py           # ConsensusTrace + ModelAttempt audit records
```

Touched modules — small surgical edits:

- `src/caretaker/config.py` — add `ModelPoolConfig` and `ConsensusDomainConfig`;
  extend `AgenticDomainConfig` with optional `consensus`; add a new
  `size_classifier: AgenticDomainConfig` slot on `AgenticConfig` so the
  Foundry size gate has a per-site config surface (it does not have one
  today — it's a pure-heuristic site).
- `src/caretaker/llm/router.py` — expose the existing `ClaudeClient` via a
  `for_model(model: str)` helper so the engine can call the same provider
  layer as today (the engine does NOT bypass `LLMRouter`; it threads through
  it).
- `src/caretaker/evolution/shadow.py` — `ShadowDecisionRecord` gains a
  `consensus_trace_json: str | None` field so disagreement / enforced records
  carry the full per-model trace.
- `src/caretaker/pr_agent/readiness_llm.py` — `evaluate_pr_readiness_llm` calls
  `engine.decide(...)` instead of `claude.structured_complete(...)` directly.
  Function signature unchanged.
- `src/caretaker/foundry/size_classifier.py` — gains a borderline-zone async
  decision path that calls the engine when count gates land between
  `borderline_low_*` and `borderline_high_*` thresholds. Below the low band
  → always `ROUTE_FOUNDRY`; above the high band → always `ESCALATE_COPILOT`.
- `src/caretaker/foundry/dispatcher.py` — switches the size-classifier calls
  from sync to async so the borderline LLM path can run.

### Public API

```python
result: ConsensusResult[T] = await engine.decide(
    site_name="readiness",                # matches AgenticConfig field
    schema=Readiness,                     # pydantic model the verdict conforms to
    system_prompt=_READINESS_SYSTEM,      # cache-friendly stable prefix
    user_prompt=build_readiness_prompt(context),
    feature="readiness",                  # for LLMRouter feature gating
)
# result.verdict: T (the consensus verdict)
# result.trace:   ConsensusTrace (per-model votes, escalation path, latencies)
```

The trace is returned to the caller so the wrapping `@shadow_decision`
decorator can serialise it onto the `ShadowDecisionRecord` it already writes —
no new persistence path.

On total LLM failure (every tier exhausted), `engine.decide` raises a typed
`ConsensusUnavailable`. The decider returns `None`, the `@shadow_decision`
wrapper falls through to the legacy heuristic, and the heuristic's verdict
ships. Per-site fallback policies:

- `readiness`: legacy fallback returns "block-merge" (conservative — never
  ship a merge on a degraded path).
- `size_classifier`: legacy fallback returns the deterministic count-gate
  verdict (the current code, kept lean).

### Strategies

Two strategies in this PR; both implement `async def run(ctx) -> ConsensusResult`:

**`TieredConfidence`** (default).
1. Call primary model.
2. If `verdict.confidence >= confidence_threshold`, ship.
3. Else call escalation tier (1–N models, in declared order). Take the
   highest-confidence verdict from the escalation tier; record the trace.
4. If every tier errors, raise `ConsensusUnavailable`.

**`AlwaysTwoModels`**.
1. Call primary + secondary in parallel.
2. If they agree on the closed-enum field(s) declared by the site (e.g.
   `Readiness.verdict` ∈ {`ready`, `not_ready`, `needs_human`}), ship the
   primary's full verdict.
3. If they disagree, escalate to a tiebreaker model and ship its verdict;
   mark `escalated=True` and increment the disagreement counter.
4. If a model errors, count it as one of the two votes failing; promote the
   tiebreaker call from "on disagreement" to "on tie/missing-vote." If both
   primary and secondary error, raise `ConsensusUnavailable`.

The set of fields used for "agreement" in `AlwaysTwoModels` is part of the
site's per-domain consensus config (`agreement_fields: list[str]`). Empty list
means "compare full verdicts via `==`." Operators set it explicitly when the
schema has noisy free-text fields (rationale, summary) that should not count
as disagreement; for `readiness` it is set to `["verdict"]` so the closed-enum
verdict field is the only thing that has to match.

Strategies are registered in `engine.py` via a small registry dict so adding
a third strategy in a follow-up PR does not require ripping the engine open.

### Provider pool — capability tags

`LLMConfig` gains:

```python
class ModelPoolConfig(StrictBaseModel):
    pool: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Capability tag → concrete model string. Tags are operator-defined; "
            "common values: 'fast', 'reasoning', 'cheap', 'vision'. Any literal "
            "model string accepted by the LLM router is a valid value."
        ),
    )
```

`ConsensusDomainConfig.primary` and `ConsensusDomainConfig.escalation[]` accept
either a tag (`"fast"`, `"reasoning"`) or a literal model string
(`"claude-sonnet-4-6"`, `"openai/gpt-4o"`). The `ProviderPool` resolver checks
the pool first; on miss, the value is treated as a literal and passed through
to the existing `LLMRouter` / `ClaudeClient`.

**Why tags**: when `claude-sonnet-4-6` is superseded six months from now, the
operator updates `pool.fast = "claude-sonnet-5-x"` once and every site picks
up the new model. No per-site config edits.

### Per-site config

```python
class ConsensusDomainConfig(StrictBaseModel):
    strategy: Literal["tiered_confidence", "always_two_models"] = "tiered_confidence"
    primary: str = "fast"                                       # tag or literal
    escalation: list[str] = Field(default_factory=lambda: ["reasoning"])
    confidence_threshold: float = 0.7
    agreement_fields: list[str] = Field(default_factory=list)   # AlwaysTwoModels only
```

```python
class AgenticDomainConfig(StrictBaseModel):
    mode: Literal["off", "shadow", "enforce"] = "off"
    model_override: str | None = None
    max_tokens_override: int | None = None
    consensus: ConsensusDomainConfig | None = None              # NEW — opt-in per site
```

When `consensus` is `None`, the existing single-model path runs (no engine
involved). Sites opt in by setting `consensus` in config.

Default `caretaker_config.yaml.example` ships:
- `agentic.readiness.mode = enforce`
- `agentic.readiness.consensus = ConsensusDomainConfig(strategy="always_two_models", primary="reasoning_anthropic", escalation=["reasoning_alt"], agreement_fields=["verdict"])`
- `agentic.size_classifier.consensus = ConsensusDomainConfig(strategy="tiered_confidence", primary="fast", escalation=["reasoning_anthropic"], confidence_threshold=0.7)`
- `llm.model_pool.pool = {"fast": <cheap-fast model>, "reasoning_anthropic": <Claude reasoning-class>, "reasoning_alt": <a different provider's reasoning-class — e.g. an OpenAI / Gemini / Foundry model via LiteLLM>, "cheap": <cheap fallback>}`

The two distinct reasoning tags (`reasoning_anthropic` and `reasoning_alt`)
deliberately resolve to two different providers' reasoning-class models. This
gives `readiness` cross-provider agreement on day one — a confidently-wrong
Anthropic model and a confidently-wrong OpenAI/Gemini model on the same prompt
are uncorrelated failure modes, which is the point of the consensus.

Per-deploy configs override these freely.

### Data flow (one decision, `readiness` example)

1. `pr_agent/agent.py` builds the readiness context, calls the existing
   `@shadow_decision("readiness")`-wrapped function with `legacy=...` and
   `candidate=evaluate_pr_readiness_llm`.
2. With `mode == "enforce"`, the wrapper calls `evaluate_pr_readiness_llm`
   first. That function builds the engine request and `await
   engine.decide(...)`.
3. The engine reads `config.agentic.readiness.consensus`, resolves the
   strategy (`AlwaysTwoModels`) and provider tags (`reasoning` →
   `claude-opus-4-7`, then `reasoning` again → next reasoning-tagged model),
   then runs the strategy.
4. Each model call goes through the existing `ClaudeClient.structured_complete`
   path; per-model latency, model id, confidence, and verdict summary are
   captured in `trace.attempts`.
5. The strategy returns `ConsensusResult(verdict, trace)`. The decider returns
   the verdict; the wrapper writes a `ShadowDecisionRecord` with
   `outcome="enforced_candidate"`, `consensus_trace_json=trace.model_dump_json()`,
   and increments `caretaker_shadow_decisions_total{outcome="enforced_candidate"}`.
6. On `ConsensusUnavailable`: decider returns `None`, the wrapper falls through
   to legacy, the legacy verdict ships, the wrapper writes
   `outcome="candidate_error"` with the engine's error reason in
   `disagreement_reason`. `caretaker_consensus_unavailable_total{site="readiness"}`
   increments.

### Error handling

| Failure mode | Behaviour |
|--------------|-----------|
| Single model error | `LLMRouter`'s existing `fallback_models` chain retries within that tier. Strategy moves on to next tier when retries exhausted. |
| Tier exhausted | `TieredConfidence` escalates to next tier; `AlwaysTwoModels` promotes tiebreaker call. |
| All tiers exhausted | `engine.decide` raises `ConsensusUnavailable`. Caller-decider returns `None`. Shadow wrapper falls through to legacy. |
| Verdict schema mismatch | Treated as a transient model error for that attempt (same path as `StructuredCompleteError`); strategy retries via the LLM router. Not a code bug. |
| `AlwaysTwoModels` disagreement | Not an error; tiebreaker runs. Counter `caretaker_consensus_disagreement_total{site}` increments so operators see how often two-model voting catches things. |
| Tag resolves to no available model | `ConfigError` at engine construction time (fail-fast — this is a deploy-time issue). |

### Observability

Three new Prometheus counters:
- `caretaker_consensus_decisions_total{site, strategy, outcome}` — outcome ∈
  {`primary_shipped`, `escalated`, `tiebreaker_shipped`, `unavailable`}.
- `caretaker_consensus_disagreement_total{site}` — increments on every
  `AlwaysTwoModels` disagreement that triggers a tiebreaker.
- `caretaker_consensus_unavailable_total{site}` — increments on every
  `ConsensusUnavailable` raise.

One new histogram:
- `caretaker_consensus_decision_seconds{site, strategy}` — end-to-end engine
  latency including all model calls.

The `ConsensusTrace` JSON blob is the authoritative per-decision audit trail —
serialised onto `ShadowDecisionRecord.consensus_trace_json` and reachable via
the existing admin shadow API.

## Testing strategy

**Unit tests** (`tests/test_consensus_engine.py`):

- `TieredConfidence` ships primary's verdict when above threshold (no second
  call made — assert via call-count on the fake client).
- `TieredConfidence` escalates when below threshold; result reflects
  escalated model's verdict.
- `AlwaysTwoModels` agreement → primary's verdict shipped, no third call.
- `AlwaysTwoModels` disagreement → tiebreaker invoked, tiebreaker's verdict
  shipped, `escalated=True` in trace.
- `AlwaysTwoModels` with one model erroring → tiebreaker promoted to second
  vote, decision still ships.
- `ConsensusUnavailable` raised when every tier errors.
- `ProviderPool` resolves tags to literal models; literal models pass through
  unchanged; unknown tag with no fallback raises `ConfigError`.
- `ConsensusTrace` serialisation roundtrips through `model_dump_json` /
  `model_validate_json`.

**Integration tests** for the two proof sites:

- `tests/test_pr_readiness_consensus.py` — fake `ClaudeClient` returns canned
  `Readiness` verdicts; assert that with `readiness.mode=enforce` and a
  configured consensus, the engine path is taken and the verdict reaches the
  caller. Inject a `ConsensusUnavailable` and assert the legacy fallback runs
  and "block-merge" (`verdict="not_ready"`) is returned.
- `tests/test_size_classifier_consensus.py` — assert the deterministic floor
  (counts above high threshold = always escalate; counts below low threshold =
  always Foundry); borderline counts trigger engine; engine failure falls back
  to the current count gate.

**Eval (deferred)**: capture `ConsensusTrace` records and add a Braintrust
evaluation in the existing `caretaker.eval` infra. Tracked as a follow-up;
the trace JSON shape is stable enough that the harness can be added later
without backfill.

## Two proof sites — concrete edits

### `readiness` (judgment replacement)

- `pr_agent/readiness_llm.py::evaluate_pr_readiness_llm` — body changes to
  call `engine.decide(...)` instead of `claude.structured_complete(...)`.
  Function signature unchanged.
- `pr_agent/agent.py` call site — no change. Existing
  `@shadow_decision("readiness")` wrapper continues, with `mode=enforce`
  becoming the default in `caretaker_config.yaml.example`. Per-deploy configs
  still control the live mode.
- Default `ConsensusDomainConfig` for `readiness`: `strategy=always_two_models`,
  `primary=reasoning_anthropic`, `escalation=[reasoning_alt]`,
  `agreement_fields=["verdict"]`. This honours the irreversibility rule: two
  reasoning-class models from **different providers** must agree on the
  verdict field before caretaker proceeds.

### `size_classifier` (heuristic augmentation)

- `foundry/size_classifier.py` — gains async `decide_pre(...)` and
  `decide_post(...)` functions wrapping the existing pure `pre_flight` /
  `post_flight`. Each gains config knobs `borderline_low_files`,
  `borderline_high_files`, `borderline_low_lines`, `borderline_high_lines`.
  Pure functions stay; the wrappers add the borderline-zone engine call.
- `foundry/dispatcher.py` — currently calls `pre_flight` / `post_flight`
  synchronously. Becomes `await classifier.decide_pre(...)` so the
  borderline LLM path can run; non-borderline calls return immediately
  without an LLM call.
- Default `ConsensusDomainConfig` for `size_classifier`:
  `strategy=tiered_confidence`, `primary=fast`, `escalation=["reasoning_anthropic"]`,
  `confidence_threshold=0.7`. Cheap by default; strong model only on
  borderline cases with low confidence.

## Migration / rollout plan

1. **Land this PR** with `agentic.readiness.consensus` and
   `agentic.size_classifier.consensus` configured as above; `readiness.mode =
   enforce` on day one for the proof site.
2. **Soak for ~2 weeks** on caretaker's own repo plus the partial-working
   fleet members (Example-React-AI-Chat-App + audio_engineer) before the
   per-deploy default flips to `enforce` everywhere.
3. **Follow-up PR**: delete `pr_agent/states.py`'s 10/20/30/40 score code
   once the soak shows zero `candidate_error` outcomes for `readiness`.
4. **Follow-up PR**: migrate the next four `@shadow_decision` sites
   (`ci_triage`, `review_classification`, `issue_triage`, `executor_routing`)
   onto the engine, opting `executor_routing` into `always_two_models` as
   another irreversible-action site.
5. **Follow-up PR (longer horizon)**: add the standalone `@consensus_decision`
   decorator and the threshold-learner once shadow data justifies them.

## Config validation

`ConsensusDomainConfig` validates at load time:

- When `strategy == "always_two_models"`, `escalation` must contain at least
  one entry, and `escalation[0]` must resolve (after pool tag-substitution)
  to a different concrete model than `primary`. This prevents an operator
  from accidentally pointing the "two-model agreement" at a single model
  consulted twice.
- `confidence_threshold` is in `[0.0, 1.0]`.
- Every tag referenced in `primary` / `escalation` either appears in
  `llm.model_pool.pool` or is a literal model string accepted by the LLM
  router. `caretaker doctor` checks this at startup and surfaces the
  misconfiguration as a fail-fast error.

## Open questions

- **Cost monitoring**. `AlwaysTwoModels` for `readiness` doubles per-decision
  token cost. Worth a per-site monthly token budget alarm at the router
  level before this lands? *Suggested: not in this PR; the existing
  `caretaker_llm_tokens_total{provider, model, feature}` counter already
  enables a Grafana alert. Open a tracking issue.*

## References

- [2026-Q2-agentic-migration.md](2026-Q2-agentic-migration.md) — the existing
  shadow-mode migration this design extends.
- `src/caretaker/evolution/shadow.py` — the `@shadow_decision` decorator and
  `ShadowDecisionRecord` model.
- `src/caretaker/config.py` — `AgenticConfig`, `LLMConfig`, current per-site
  `model_override` plumbing.
- `src/caretaker/llm/router.py` — current `LLMRouter` / `ClaudeClient`
  provider-routing layer the engine reuses.
