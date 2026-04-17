# Azure backend adoption roadmap

Companion to:

- [`azure-mcp-architecture-plan.md`](./azure-mcp-architecture-plan.md) — hosting decision and phasing principles.
- [`azure-backend-expansion-brainstorm.md`](./azure-backend-expansion-brainstorm.md) — full menu of services and features.

This document **stack-ranks those options by ROI** and groups them into
adoption phases. Each phase lists goals, deliverables, dependencies, exit
criteria, and rough sizing so we can commit phase-by-phase instead of
taking on everything at once.

Guiding principles carried over from the architecture plan:

1. Backward compatibility first — local CLI and GitHub Actions must keep
   working at every phase.
2. Additive config only — new services appear as opt-in sections in
   `MaintainerConfig`, default off.
3. No phase forces the next — each phase must pay for itself.
4. Prefer one-DB / one-cache simplicity until proven inadequate.

---

## ROI stack-ranking (biggest-bang-first)

Ranked by (impact) × (unblocks future work) ÷ (cost + operational
complexity). Higher rank = do sooner.

| Rank | Item                                                                 | Primary payoff                                           | Unblocks                                               | Rough effort |
| ---- | -------------------------------------------------------------------- | -------------------------------------------------------- | ------------------------------------------------------ | ------------ |
| 1    | **App Insights + OpenTelemetry end-to-end tracing**                  | Debuggability + proof-of-value for everything else       | Every later phase (safe rollout, alerts, eval)         | S            |
| 2    | **`MemoryBackend` protocol abstraction**                             | Removes SQLite hardwiring                                | Postgres, multi-replica, durable audit log             | S            |
| 3    | **Azure Database for PostgreSQL Flexible Server (Burstable)**        | Durable, multi-replica system of record                  | Audit log, Run API, scheduler, multi-tenant, pgvector  | M            |
| 4    | **Azure Cache for Redis Basic C0**                                   | Safe horizontal scale; installation-token cache          | Multi-replica dedup, background worker, rate limiting  | S            |
| 5    | **Key Vault + Managed Identity (AKS Workload Identity) for secrets**| Removes env-var secret footgun; enables APIM later       | APIM AI Gateway, external consumers, compliance        | M            |
| 6    | **GitHub App installation-token broker (Redis + Key Vault)**         | One fresh scoped token per run; centralizes JWT          | Multi-process safety, rotation, per-install isolation  | S/M          |
| 7    | **Async Run API + background worker** (`POST /runs`, SSE events)     | Webhook <10 s SLA; durable retries; foundation for UI    | `caretaker runs` CLI, dashboard, scheduler             | M            |
| 8    | **Structured audit log table + LLM cost/usage capture**              | Compliance + eval + cost visibility                      | Workbooks dashboards, budget alerts                    | S            |
| 9    | **Client CLI: `runs list/show/tail`, `doctor`, profiles, `--remote`**| Dev ergonomics; onboarding; teammates without API keys   | Broader adoption                                       | S            |
| 10   | **Azure OpenAI provider in `LLMRouter` + prompt/response Blob logs** | Second LLM provider under existing router; eval-ready    | APIM AI Gateway, eval harness                          | S/M          |
| 11   | **Eval harness (Postgres dataset + nightly run)**                    | Quantitative prompt quality regression protection        | Safe prompt/model changes                              | M            |
| 12   | **Scheduled-run engine (Postgres-backed)**                           | Repos without GitHub Actions can still run caretaker     | Multi-tenant hosted mode                               | S/M          |
| 13   | **Semantic memory (pgvector first, AI Search Basic if needed)**      | Precedent-based PR review, CI failure recall             | Smarter agents, fewer duplicate actions                | M            |
| 14   | **APIM as AI Gateway in front of MCP + LLMs**                        | Governance, quotas, semantic cache, content safety       | External consumers, multi-provider load balancing      | M/L          |
| 15   | **Azure Service Bus event bus** (`pr.opened`, `ci.failed`, …)        | Decoupled fan-out, DLQ, replay                           | Multi-consumer, long-running workflows                 | M            |
| 16   | **Multi-tenant installation isolation** (RLS / schema-per-install)   | Safe hosted SaaS mode                                    | External GitHub App distribution                       | L            |
| 17   | **Content Safety / PII redaction on outbound GitHub writes**         | Responsible AI on public surfaces                        | Public-GitHub trust                                    | S/M          |
| 18   | **Azure AI Foundry hosted agents / prompt optimizer**                | Managed eval + prompt tuning                             | Only if eval harness in #11 proves insufficient        | M            |

Effort key: **S** ≈ ≤1 week, **M** ≈ 1–3 weeks, **L** ≈ >3 weeks (single
engineer).

---

## Phase 0 — Telemetry and seams first (no new Azure spend)

**Goal:** make the system observable and pluggable before adding any
new service dependencies.

**Scope (rank 1–2):**

- App Insights connection string wired through the existing
  [`mcp/telemetry.py`](../src/caretaker/mcp/telemetry.py) stub.
- OpenTelemetry instrumentation on:
  - FastAPI in `mcp_backend/main.py`
  - `Orchestrator.run()` and each `BaseAgent.run()`
  - `LLMRouter` calls
  - MCP client HTTP calls
- Correlation ID propagation: `X-GitHub-Delivery → run_id → agent_id →
  tool_call_id → llm_call_id` as span attributes.
- Introduce `MemoryBackend` protocol. Keep the SQLite implementation as
  the default; add a stub interface for future Postgres/Redis backends.
- Additive config section: `telemetry.enabled`,
  `telemetry.application_insights_connection_string_env`,
  `memory.backend` (`"sqlite" | "postgres"`).

**Deliverables:**

- OTel wiring PR.
- `MemoryBackend` abstraction PR (no behavior change).
- Dashboard: one App Insights workbook with runs/min, error rate,
  top-N agents by latency.

**Exit criteria:**

- Every CLI run and every webhook call produces a single App Insights
  trace with correct parent/child spans.
- All existing tests pass with `telemetry.enabled=false` (default).
- SQLite path unchanged; abstraction has one real + one fake backend
  used in tests.

**Cost:** App Insights ingestion only — pennies until volume grows.

---

## Phase 1 — Durable state and cache (unlocks multi-replica)

**Goal:** move from "single-process SQLite" to a durable, multi-replica
backend without changing the orchestrator contract.

**Scope (rank 3–4, 6, 8):**

- **Azure Database for PostgreSQL Flexible Server — Burstable B1ms**
  (Entra ID passwordless auth, private endpoint, daily backups).
  - Schema: `runs`, `agent_runs`, `actions`, `audit_log`,
    `webhook_deliveries`, `memory_blobs`.
  - Alembic migrations in-repo.
- **Postgres-backed `MemoryBackend`** implementation.
- **Azure Cache for Redis Basic C0** (private endpoint, Entra auth).
- **Redis-backed webhook dedup** replacing the in-process deque.
- **Installation-token broker**: JWT signed with a Key Vault-referenced
  key (still env-var OK in phase 1), cached in Redis, issued via a
  small `/internal/tokens/installation/{id}` endpoint.
- **Audit log writer** in the orchestrator: one row per agent decision
  including tool, LLM, prompt/response IDs, latency, cost, outcome.

**Deliverables:**

- Bicep or Terraform module for Postgres + Redis + private endpoints.
- Migration path doc: SQLite → Postgres (dump + import, idempotent).
- Feature flags: `postgres.enabled`, `redis.enabled` — both default off.
- New tests for multi-replica dedup and token broker concurrency.

**Exit criteria:**

- Caretaker can run 2+ backend replicas without double-processing
  webhooks.
- Every run produces audit-log rows queryable from App Insights
  workbooks and from Postgres directly.
- Token broker survives kill-and-restart within TTL.

**Cost estimate:** ~$30–60/mo (Postgres B1ms + Redis C0 + Private
Endpoints). Can pause Postgres in non-prod.

---

## Phase 2 — Secrets, identity, and operational hygiene

**Goal:** remove env-var secrets and make the footprint compliance-ready.

**Scope (rank 5):**

- **Key Vault + CSI Secrets Store driver** on AKS.
- **AKS Workload Identity** for the MCP backend deployment.
- Rotate: GitHub App private key, webhook secret, Claude/Copilot/AOAI
  keys into Key Vault; remove from ConfigMaps/Secrets.
- **Private endpoints** on Postgres, Redis, Key Vault, Storage.
- **Dual-key rotation** support for the GitHub App signing key (two
  active keys; new-key-wins verification).
- Azure Monitor alerts: webhook 5xx, LLM 4xx/5xx, Postgres connections
  saturated, Redis memory > 70%, Key Vault secret within 30 days of
  expiry.

**Deliverables:**

- IaC module for Key Vault + Workload Identity + RBAC assignments.
- Rotation runbook.
- Alert rule definitions in IaC.

**Exit criteria:**

- No long-lived secrets in k8s Secret objects or env vars for the
  backend deployment.
- `kubectl get secrets` in the caretaker namespace shows only
  bootstrap/system secrets.
- Rotation test passes without downtime.

**Cost estimate:** Key Vault + Private Endpoints ~$10–20/mo.

---

## Phase 3 — Run API and client ergonomics (product surface)

**Goal:** turn caretaker into a queryable service and make the CLI
pleasant for teammates without API keys locally.

**Scope (rank 7, 9):**

- Backend:
  - `POST /runs` — enqueue a run (background worker consumes).
  - `GET /runs/{id}` — status + summary.
  - `GET /runs/{id}/events` — SSE event stream.
  - Background worker: Postgres `SKIP LOCKED` queue (defer Celery
    until we have multiple queues).
  - Per-tool authorization scopes (extend the existing auth modes).
- Client:
  - `caretaker runs list|show|tail` backed by the new API.
  - `caretaker run --remote` proxy mode.
  - `caretaker doctor` — probes MCP, Postgres, Redis, Key Vault; lists
    expiring secrets.
  - `caretaker --profile staging|prod` config profiles.
  - MCP tool discovery caching keyed on endpoint + version.

**Deliverables:**

- Run API + worker PR.
- CLI command additions with unit + e2e tests.
- Client-facing API docs under `docs/`.

**Exit criteria:**

- A webhook returns 200 in <500 ms; run completes asynchronously and
  is fully visible via the Run API.
- `caretaker doctor` produces a single-page readiness report.
- `--remote` works against both staging and prod profiles.

**Cost estimate:** marginal — same Postgres + Redis.

---

## Phase 4 — LLM provider breadth and evaluation

**Goal:** add Azure OpenAI cleanly and protect prompt quality with
automated eval.

**Scope (rank 10–11):**

- `AzureOpenAIProvider` added to `LLMRouter` (managed-identity auth
  where supported, key-based fallback via Key Vault).
- Prompt + response Blob logging (Standard LRS, lifecycle to
  cool/archive). Keyed by `run_id` for traceability.
- **Eval harness**:
  - Postgres tables `eval_datasets`, `eval_runs`, `eval_results`.
  - Nightly GitHub Actions job: sample N historical PRs/issues, run
    current prompts, compare to recorded ground truth, store results.
  - App Insights workbook trending eval scores per agent/prompt
    version.

**Deliverables:**

- Azure OpenAI provider + routing config.
- Eval harness service + CLI (`caretaker eval run|show|diff`).
- Initial seed dataset curated from existing audit-log data.

**Exit criteria:**

- Switching primary provider between Claude and Azure OpenAI is a
  config change only.
- Any prompt change ships with an eval delta.

**Cost estimate:** AOAI pay-as-you-go based on usage; Blob <$5/mo at
expected volume.

---

## Phase 5 — Scheduling and semantic memory

**Goal:** extend caretaker's reach beyond event-driven and add
precedent-based intelligence.

**Scope (rank 12–13):**

- **Postgres-backed scheduler** (`scheduled_runs` table with cron
  expressions) so repos without GitHub Actions cron can still run
  agents.
- **pgvector extension** on the existing Postgres — semantic memory
  v1:
  - embeddings for PRs (title + body + diff summary), issues, and
    CI failure signatures.
  - retrieval tool exposed via MCP: `find_precedents(query, k=5)`.
- Upgrade path documented: pgvector → Azure AI Search Basic if quality
  or scale demands it (same retrieval API).

**Deliverables:**

- Scheduler worker + CLI (`caretaker schedules add|list|remove`).
- Embedding pipeline + nightly backfill job.
- New MCP tool registered in `/mcp/tools`.

**Exit criteria:**

- A scheduled run fires reliably and is visible via Run API.
- Precedent tool surfaces at least one relevant prior artifact in 80%+
  of sampled runs (measured via eval harness).

**Cost estimate:** pgvector adds ~0; embedding calls depend on
provider (cheap at current scale).

---

## Phase 6 — Governance gateway and multi-consumer readiness

**Goal:** prepare caretaker for more than one external consumer
(internal platform tenant, second product, OSS hosted tenant, etc.).

**Scope (rank 14–15, 17):**

- **Azure API Management (Developer or Basic v2)** in front of `/mcp`
  and the LLM router's upstream providers.
  - Policies: bearer/JWT auth, per-consumer quotas, semantic cache on
    idempotent tool calls, token-limit policy on LLM calls, content
    safety, jailbreak detection.
- **Azure Service Bus** topics: `pr.opened`, `issue.created`,
  `ci.failed`, `run.completed`. Agents become subscribers. DLQ and
  replay enabled.
- **Content Safety / PII redaction** applied to outbound comments
  and issues.

**Deliverables:**

- APIM IaC + policy files under `infra/`.
- Producer/consumer wiring for at least one event end-to-end.
- Redaction module integrated into the GitHub write paths.

**Exit criteria:**

- A second consumer can be onboarded via APIM subscription without
  backend code changes.
- One complete async workflow runs end-to-end through Service Bus.

**Cost estimate:** APIM Developer ~$50/mo (non-prod) or Basic v2 ~$100/mo;
Service Bus Standard ~$10/mo base + usage.

---

## Phase 7 — Hosted multi-tenant mode (only if productized)

**Goal:** support caretaker as a multi-install hosted GitHub App
safely.

**Scope (rank 16):**

- Installation-scoped data isolation: row-level security or
  schema-per-install in Postgres.
- Per-install rate limits (APIM + Redis counters).
- Per-install Key Vault scopes; per-install billing/usage export.
- Admin API for install lifecycle (enable/disable/suspend).

**Exit criteria:**

- A rogue install cannot affect or read another install's data,
  verified by automated tests.
- Per-install cost attribution works.

**Cost estimate:** scale-dependent; mostly engineering cost.

---

## Phase 8 — Advanced (only if justified by data)

Reserve this for items that need demonstrated demand:

- **Azure AI Foundry hosted agents / prompt optimizer** (rank 18) —
  only if eval harness proves our in-repo tuning is a bottleneck.
- Graph DB or ADX/Fabric analytics — only if relationship traversal or
  BI clearly pay off.
- PTUs for Azure OpenAI — only at sustained high volume.

---

## Cross-phase tracking

Every phase should produce:

1. A small ADR under `docs/adr/` capturing *why now* and *success
   metric*.
2. An IaC PR (Bicep or Terraform module).
3. A code PR flagged behind a default-off config toggle.
4. A rollback plan documented before merge.
5. A single App Insights workbook tile proving the phase delivered
   its stated payoff.

### Suggested cadence

- Phases 0–2: back-to-back, roughly one phase per 2-week cycle.
- Phase 3: next cycle after phase 2 stabilizes.
- Phase 4 onward: prioritized based on real usage signals rather than
  calendar.

### Gate checks before starting each phase

- Previous phase's exit criteria fully met.
- No unresolved P1/P2 alerts in the last 7 days tied to previous phase.
- Config schema additions merged and documented in
  [`docs/configuration.md`](./configuration.md).

---

## Summary

The ordering above front-loads **observability (phase 0)** so every
later change is measurable, then delivers the **biggest single
architectural unblock (Postgres + Redis + MemoryBackend, phases 1–2)**,
then turns caretaker into an **actual service with a Run API and good
client UX (phase 3)**, then broadens **LLM options and quality guard
rails (phase 4)**, and only then touches **advanced capabilities
(phases 5–8)** that each require justification.

This keeps every phase small, reversible, and opt-in, while ensuring
the highest-ROI moves happen first.
