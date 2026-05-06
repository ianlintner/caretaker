# Observability Improvements — Lessons from v0.28 QA

**Status:** Proposed plan
**Author:** drafted 2026-05-06 after the v0.28.0 → v0.28.4 live-fire QA cycle
**Owner:** TBD
**Related work:** v0.28.x release series (PRs #674, #677, #679, #681, #683)

---

## 1. Why this plan exists

Shipping `opencode_local` to caretaker-qa took five patch releases (v0.28.1 → v0.28.4) because the failure modes were almost all silent:

| # | Bug | Failure signal we *had* | Failure signal we *needed* |
|---|---|---|---|
| 1 | `opencode -p` is wrong syntax | "Empty review body" on PR | An `opencode_invocations_total{outcome="cli_help_returned"}` counter |
| 2 | Bogus model IDs | None (parser fell back to COMMENT review) | A loud log + metric when fallback parser fires |
| 3 | `git` not in image | `[Errno 2] No such file or directory: 'git'` *eventually* surfaced as a comment | A `/health/deep` self-test that would have caught it pre-deploy |
| 4 | Backend stdout silent | Pod logs only showed startup messages | DEBUG-level env var (added in v0.28.3) — needs default-INFO with structured fields |
| 5 | `gemini-3-pro-preview` rejected by OpenRouter | "No endpoints found" buried in subprocess WARNING line | Pre-flight model probing in CI + alerts on first runtime failure |

Every one of these would have been a 5-minute fix if the right signal existed. Most of the time on the v0.28.x cycle was spent piecing together what the backend was doing rather than fixing actual code.

---

## 2. What we already have (don't rebuild)

- **Prometheus metrics** at `:9090` — ~30 counters/histograms for HTTP, eventbus, fix-ladder, LLM cache, etc. (`src/caretaker/observability/metrics.py`)
- **OTEL traces → Tempo** — `caretaker-mcp` is on the always-keep sampling list; auto-instrumentors for FastAPI, httpx, Redis, Neo4j (`src/caretaker/observability/bootstrap.py`)
- **Trace ID stamping on log records** — every `LogRecord` gets `otelTraceID` / `otelSpanID` (same module)
- **Admin dashboard** at `caretaker.cat-herding.net` — has read-only state/runs/PR endpoints behind OIDC
- **CARETAKER_LOG_LEVEL env var** (added v0.28.3) — controls Python root logger

The plan below builds on all of this. No new observability stack.

---

## 3. Phase 1 — Quick wins (1 day total, high signal)

These are the items that would have caught all five v0.28.x bugs. Implement first.

### 3.1 Per-PR audit log line at INFO

Today's logs are either silent (INFO) or a firehose (DEBUG). Add **one summary line per PR review** at INFO that captures the full decision in a single line that can be `grep`'d:

```
INFO caretaker.pr_reviewer.agent: pr_review_complete repo=ianlintner/caretaker-qa pr=108 author=ianlintner is_caretaker_owned=False routing=score=0_inline tier=None backend=opencode_local model=openrouter/google/gemini-2.5-pro verdict=REQUEST_CHANGES duration_ms=42103 issue_categories=[lint,format,correctness] auto_fix_dispatched=False auto_fix_reason="not caretaker-owned"
```

**Where:** `src/caretaker/pr_reviewer/agent.py:_handle_pr` — log this line right before `return`. Use `logger.info` with `extra={...}` so structured-log consumers (Tempo logs, Grafana Loki) get fields, not a giant string.

### 3.2 Loud fallback-parser warning

The `opencode_local` fallback parser silently produced a useless review on bug #2 and #5. Add:

```python
# in opencode_local._parse_review_payload, fallback branch
logger.warning(
    "opencode_local: fallback parse used — structured caretaker-review JSON missing. "
    "stdout_bytes=%d sample=%r",
    len(assistant_text), assistant_text[:200],
)
record_error("pr_reviewer", "opencode_local_fallback_parse")
```

Counter the fallback in Prometheus too (see 3.4).

### 3.3 `/health/deep` endpoint

A new readiness-style endpoint that probes external dependencies and returns a structured health verdict. Would have caught bugs #3 and #5 before deploy.

```json
GET /health/deep
{
  "status": "ok|degraded|fail",
  "checks": {
    "git_cli": {"status": "ok", "version": "2.47.3"},
    "opencode_cli": {"status": "ok", "version": "1.14.39"},
    "redis": {"status": "ok", "latency_ms": 3},
    "mongo": {"status": "ok"},
    "neo4j": {"status": "ok"},
    "openrouter_models": {
      "status": "degraded",
      "configured_models": [...],
      "probed_at": "2026-05-06T14:00:00Z",
      "results": {
        "openrouter/google/gemini-2.5-pro": "ok",
        "openrouter/google/gemini-3-pro-preview": "fail: No endpoints found"
      }
    }
  }
}
```

**Where:** new endpoint in `src/caretaker/mcp_backend/main.py`. Rate-limit the OpenRouter model probe (hourly cache) so it doesn't burn tokens on every kubelet probe.

### 3.4 Five new Prometheus counters

Bolt-on additions to `observability/metrics.py`:

```python
PR_REVIEW_OUTCOME_TOTAL = Counter(
    "caretaker_pr_review_outcome_total",
    "PR review outcomes by backend, tier, verdict",
    ["backend", "tier", "verdict"],  # tier in {trivial,simple,standard,complex,none}
)
PR_REVIEW_DURATION_SECONDS = Histogram(
    "caretaker_pr_review_duration_seconds",
    "End-to-end PR review duration",
    ["backend", "tier"],
    buckets=[1, 5, 10, 30, 60, 120, 300, 600],
)
COMPLEXITY_CLASSIFIER_TIER_TOTAL = Counter(
    "caretaker_complexity_classifier_tier_total",
    "Complexity classifier verdicts by tier and decision source",
    ["tier", "source"],  # source in {fast_path, llm, heuristic_fallback}
)
OPENCODE_INVOCATION_TOTAL = Counter(
    "caretaker_opencode_invocation_total",
    "opencode CLI subprocess invocations by model and outcome",
    ["model", "mode", "outcome"],  # mode={review,fix}, outcome={ok,parse_fallback,timeout,no_endpoints,error}
)
AUTO_FIX_DISPATCH_TOTAL = Counter(
    "caretaker_auto_fix_dispatch_total",
    "Auto-fix dispatcher decisions",
    ["backend", "category", "outcome"],  # outcome={dispatched_success,dispatched_fail,skipped}
)
```

Wire into the agent at the same call sites the audit log line goes (3.1).

---

## 4. Phase 2 — Per-PR timeline endpoint (~1 day)

Today, "what did caretaker do on PR #108?" requires `kubectl logs -l app=caretaker-mcp --since=1h | grep 108` across both pods. Replace with:

```
GET /api/admin/pr/{owner}/{repo}/{number}/timeline
```

Returns a chronological list of every backend decision touching that PR, drawn from MongoDB's existing `fleet_heartbeats` + a new `pr_decisions` collection that the agent writes to as it goes:

```json
[
  {"at": "2026-05-06T14:01:14Z", "agent": "pr_reviewer", "event": "review_started", "backend": "opencode_local", "tier": null, "model": "openrouter/google/gemini-2.5-pro"},
  {"at": "2026-05-06T14:01:38Z", "agent": "pr_reviewer", "event": "review_posted", "verdict": "COMMENT", "fallback": true, "duration_ms": 24000},
  {"at": "2026-05-06T14:01:45Z", "agent": "pr_agent", "event": "state_transition", "from": "discovered", "to": "review_changes_requested"},
  {"at": "2026-05-06T14:01:45Z", "agent": "pr_agent", "event": "auto_fix_skipped", "reason": "not caretaker-owned"}
]
```

**Where:** new collection `pr_decisions` (TTL 30d), write helper in `src/caretaker/state/pr_decisions.py`, endpoint in `src/caretaker/admin/api/pr_routes.py`. SPA can render this as a vertical timeline on the existing PR detail page.

### 4.1 Trace-ID linking

Each `pr_decisions` row carries `trace_id` from the active OTEL span. The admin SPA renders that as a link to Tempo (`tempo.default.svc:3200/api/traces/{trace_id}`) so a single click jumps from "we picked tier=trivial" → the full trace.

---

## 5. Phase 3 — Cost tracking (~1 day)

We just shifted models from Opus/R1 to Gemini/DeepSeek, but we have **no measurement** of whether that's actually saving money. The opencode CLI emits token counts on the final result event in stream-json mode (we can also intercept via OpenRouter's `X-Total-Tokens` headers).

```python
LLM_TOKENS_TOTAL = Counter(
    "caretaker_llm_tokens_total",
    "Tokens consumed per model and direction",
    ["model", "direction"],  # direction in {prompt, completion}
)
LLM_COST_USD_TOTAL = Counter(
    "caretaker_llm_cost_usd_total",
    "Estimated USD cost per model (using a static price table)",
    ["model"],
)
```

Static price table in `config.py` keyed by model ID; updated quarterly. Grafana dashboard panel "Per-repo daily LLM spend" answers "did the v0.28 model shift work?" in one chart.

---

## 6. Phase 4 — Tempo trace richness (~half day)

We're already sending traces but they're sparse. Add custom spans for the slow / decision-heavy operations:

| Operation | Span name | Useful attributes |
|---|---|---|
| opencode subprocess | `opencode_local.invoke` | `opencode.model`, `opencode.mode`, `opencode.workdir`, `opencode.exit_code` |
| Complexity classifier | `complexity_classifier.classify` | `pr.repo`, `pr.number`, `tier`, `source`, `confidence` |
| `prepare_workdir` | `pr_review.clone_workdir` | `pr.head_sha`, `clone_depth` |
| `dispatch_auto_fix` | `auto_fix.dispatch` | `decision.backend`, `decision.categories`, `attempt`, `outcome.success` |
| `inline_reviewer.review` | `inline_reviewer.review` | `pr.diff_lines`, `model`, `verdict` |

Each PR review then becomes a single trace tree in Tempo:

```
pr_reviewer.handle_pr (root span, repo=caretaker-qa, pr=108)
├── routing.decide
├── complexity_classifier.classify (tier=standard, source=llm)
├── opencode_local.run
│   ├── pr_review.clone_workdir (200ms)
│   └── opencode_local.invoke (model=gemini-2.5-pro, 24000ms)
├── github.create_review (verdict=REQUEST_CHANGES)
└── auto_fix.dispatch (outcome=skipped, reason=not_caretaker_owned)
```

A Grafana dashboard with "slowest reviews last 24h" + "highest-cost reviews" + "fallback-parser triggers" reads straight from these spans.

**Where:** add `tracer = trace.get_tracer(__name__)` + `with tracer.start_as_current_span(...):` blocks in five files. Mostly mechanical, ~150 LoC total.

---

## 7. Phase 5 — Alerting (1 day after Phase 1 metrics land)

Once Phase 1's counters exist, wire Prometheus alert rules. Sample set:

| Alert | Expression | Severity |
|---|---|---|
| `OpencodeFallbackParseSpike` | `rate(caretaker_opencode_invocation_total{outcome="parse_fallback"}[10m]) > 0.1` | warning |
| `OpencodeNoEndpointsSpike` | `rate(caretaker_opencode_invocation_total{outcome="no_endpoints"}[10m]) > 0.05` | critical |
| `AutoFixFailureRate` | `rate(caretaker_auto_fix_dispatch_total{outcome="dispatched_fail"}[1h]) / rate(caretaker_auto_fix_dispatch_total[1h]) > 0.3` | warning |
| `PRReviewLatencyHigh` | `histogram_quantile(0.95, caretaker_pr_review_duration_seconds_bucket) > 300` | info |
| `WebhookSignatureFailures` | `rate(caretaker_errors_total{kind="webhook_signature"}[5m]) > 0` | critical |

Notification target: GitHub Issue auto-creation in `ianlintner/caretaker` with the `meta:alert` label (uses caretaker's own issue agent — eat your own dog food).

---

## 8. Phase 6 — Local diagnostic CLI (stretch, ~half day)

```bash
caretaker debug pr-review https://github.com/ianlintner/caretaker-qa/pull/108
```

Runs the full pr_reviewer flow against the deployed backend's config but **without writing to GitHub** — prints the routing decision, complexity tier, opencode invocation, parsed review, and what auto-fix would do. Lets ops dry-run a fix-config change before pushing it.

**Where:** new `caretaker debug` subcommand group in `src/caretaker/cli.py`. Reuses the existing agent classes with a `dry_run=True` flag added to `PRReviewerAgent.execute`.

---

## 9. Sequencing

| Phase | Effort | Unblocks |
|---|---|---|
| 1. Audit log line, fallback warning, /health/deep, 5 metrics | 1 day | Catches the next v0.28.x-class bug at deploy time |
| 2. Per-PR timeline endpoint | 1 day | Replaces every "what did caretaker do on this PR?" investigation |
| 3. Cost tracking | 1 day | Answers "did the model shift save money?" |
| 4. Tempo trace richness | 0.5 day | Slow-review root-cause diagnosis |
| 5. Alerting | 1 day (after 1) | Pages on real failures, not just charts |
| 6. Local debug CLI | 0.5 day | Pre-deploy validation by ops |

Recommend landing **Phase 1 in a single PR this week** — that's where the cost/value ratio is best. Phases 2-6 can run in parallel afterward.

---

## 10. Open questions

- **Cost table maintenance:** OpenRouter prices change. Should we ingest their `/models` endpoint nightly into a Mongo collection and use that, or hand-maintain a static table? Static is simpler; ingestion is more accurate.
- **PII / secret redaction in audit log:** the per-PR timeline writes structured JSON. Need a redactor that strips `Bearer ...`, `sk-or-...` patterns, etc., before persistence.
- **Trace sampling for cost:** caretaker-mcp is "always-keep" today. With richer spans that's fine for QA but might be too much in a busy fleet. Likely want to switch to a tail-sampler that always keeps `verdict=REQUEST_CHANGES` and `outcome=parse_fallback` and probabilistically samples the rest.
- **Local logging vs OTEL log pipeline:** currently we stamp trace IDs on logs but don't ship them via OTEL. If we're using Loki/Grafana already, hooking `OTLPLogExporter` would unify "logs near traces" in Grafana — worth doing in Phase 4 alongside richer spans.

---

*End of plan.*
