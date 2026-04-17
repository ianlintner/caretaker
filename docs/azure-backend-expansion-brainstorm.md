# Azure backend expansion brainstorm

Companion to [`azure-mcp-architecture-plan.md`](./azure-mcp-architecture-plan.md).
Where that document locks in the *hosting* decision (small MCP service on
existing AKS, APIM deferred, SQLite default), this document enumerates the
**features, databases, and services** we could add to the backend and the
client to evolve caretaker into a richer Azure-hosted product — without
changing the conservative phasing.

Everything here is optional. Items are tagged by tier so we can pick and
choose based on concrete need.

## Grounding on what we already have

The current backend surface (`src/caretaker/mcp_backend/main.py`) is
intentionally tiny:

- `GET  /health`
- `GET  /mcp/tools`
- `POST /mcp/tools/call`
- `POST /webhooks/github`
- `GET  /oauth/callback`

State is a process-local LRU deque for webhook delivery dedup; there is no
durable DB, no cache, no eventing. The orchestrator is single-process,
config-driven, and most extension points are already in place:

- `AgentContext` ([`src/caretaker/agent_protocol.py`](../src/caretaker/agent_protocol.py))
- `MaintainerConfig` ([`src/caretaker/config.py`](../src/caretaker/config.py))
- `LLMRouter` ([`src/caretaker/llm/router.py`](../src/caretaker/llm/router.py))
- `MemoryStore` ([`src/caretaker/state/memory.py`](../src/caretaker/state/memory.py))
- MCP client abstractions ([`src/caretaker/mcp/`](../src/caretaker/mcp/))

Every recommendation below is designed to slot **behind** those seams, not
replace them.

---

## 1. Databases and stateful stores — ranked

### Tier A — worth adding soon

#### Azure Database for PostgreSQL Flexible Server — Burstable B1ms or B2s

- Best general-purpose system of record once we run >1 replica.
- Replaces SQLite for any durable state.
- Use for:
  - durable `MemoryStore` backend
  - run / agent / action history
  - GitHub App installation token cache
  - webhook delivery log (replaces the in-process `_seen_deliveries`
    across replicas)
  - goal state
  - structured audit log
- Pair with **Entra ID passwordless auth + managed identity**, matching the
  existing `azure.use_managed_identity` plan.
- Why not Cosmos: caretaker's data is relational
  (`run → agent → action → pr/issue`). SQL wins until massive scale.

#### Azure Cache for Redis — Basic C0 (~$16/mo)

- Needed the moment the backend has >1 replica; unlocks HA cleanly.
- Use for:
  - webhook dedup (replace in-process deque)
  - short-lived locks around PR / issue state transitions
  - GitHub installation token cache (TTL ≈ 50 min matches GitHub)
  - rate-limit counters (per installation, per tool, per LLM provider)
  - per-repo "run already in progress" flags
  - cheap LLM response cache keyed on prompt hash

#### Azure Key Vault

- Holds GitHub App private key, webhook secret, Claude / Copilot /
  Azure OpenAI keys.
- Env vars + k8s Secret work today, but Key Vault + CSI driver + managed
  identity is the Azure-idiomatic upgrade and cleanly audits rotation.

### Tier B — add when a concrete need appears

#### Azure AI Search — Basic tier *(or `pgvector` on the existing Postgres)*

- Semantic memory once we have a defined retrieval use case:
  - "have we seen this CI failure before?"
  - "find PRs similar to this one for precedent-based review"
  - "have we filed an issue like this already?"
- Basic tier gives keyword + vector + semantic hybrid in one SKU.
- `pgvector` keeps us at one DB and zero extra cost — pick that first
  unless quality/volume demand AI Search.

#### Azure Blob Storage — Standard LRS

- Cheap durable artifacts: run reports (markdown/JSON), LLM
  request/response traces, diffs beyond GitHub retention,
  `tools/debug_dump.py` outputs.
- Lifecycle policy: hot → cool at 30d → archive at 180d.

### Tier C — skip for now

- Cosmos DB (Mongo/Graph): overkill until relationship traversal dominates.
- Azure SQL / Managed Instance: wrong shape for an OSS footprint.
- Azure Data Explorer / Microsoft Fabric: premature for current reporting
  scope.

---

## 2. Backend features worth adding

Ordered by ROI.

### Near term (phase 1.5)

- **`MemoryBackend` protocol abstraction.** Small refactor; unlocks
  Postgres / Redis backends cleanly. `MemoryStore` is currently hardwired
  to SQLite.
- **Redis-backed webhook dedup** replacing the in-process deque, so
  multi-replica is safe.
- **Async run queue + background worker.** Webhook handlers must return
  <10 s to GitHub; today the design conflates receiving with processing.
  Options:
  - lightweight: Postgres table + `SELECT … FOR UPDATE SKIP LOCKED` pollers
  - medium: Celery / RQ / Dramatiq with Redis broker
  - richer: Azure Service Bus (defer until multi-consumer).
- **GitHub App installation-token broker.** Centralize JWT signing + token
  caching in the backend (Redis TTL ~50 min, Key Vault signing key). Every
  agent gets a fresh scoped token instead of each CLI process signing its
  own.
- **Structured audit log table.** One row per agent decision
  (who / when / repo / tool / LLM / cost / result). Huge future-compliance
  win; trivial to add.
- **OpenTelemetry → Application Insights.** `mcp/telemetry.py` is already
  stubbed. Wire traces as
  `webhook → run → agent → tool → LLM` with correlation IDs.

### Medium term (phase 2)

- **Run API**: `POST /runs`, `GET /runs/{id}`, `GET /runs/{id}/events`.
  Turns caretaker from CLI+webhook into a queryable service; makes any UI
  trivial.
- **SSE / WebSocket live progress stream** at `/runs/{id}/events`.
- **Per-tool authorization scopes** beyond today's `token` / `apim` binary
  (e.g. client A can `review_pr` but not `merge_pr`).
- **LLM rate-limit + circuit breakers.** Handle 429s cleanly, record in the
  audit log.
- **Scheduled-run engine.** Postgres-backed scheduler for repos that don't
  use GitHub Actions cron.
- **Key Vault secret-expiry cron.** Surfaces expiring GitHub App keys /
  LLM keys before outages.

### Longer term (phase 3+)

- **Azure API Management (AI Gateway)** in front of MCP + LLM backends:
  semantic caching, token limits, content safety, jailbreak detection,
  per-consumer quotas, multi-provider load balancing. Worth it at 2+
  external consumers.
- **Azure Service Bus event bus.** Topics per event
  (`pr.opened`, `issue.created`, `ci.failed`), agents as subscribers,
  DLQ, replay. Worth it at 2+ consumers of the same event.
- **Multi-tenant installation isolation.** Row-level security or
  schema-per-installation in Postgres; per-install rate limits and scoped
  Key Vault secrets.
- **Azure Content Safety** in the LLM router for anything written back to
  public GitHub surfaces.

---

## 3. Client / CLI features worth adding

- **`caretaker runs list/show/tail`** backed by the new Run API; tail via SSE.
- **`caretaker run --remote`** — proxy local invocations to the hosted
  backend so teammates don't need API keys locally.
- **`caretaker doctor`** — validates config, probes MCP / Postgres /
  Redis / Key Vault, reports expiring secrets. Best dev-ergonomics win
  per hour invested.
- **Config profiles** (`caretaker --profile staging|prod`) — switch
  between local, staging AKS, and prod AKS.
- **MCP tool discovery + caching** — the `/mcp/tools` endpoint is already
  the seam; clients refresh lazily instead of shipping static tool lists.
- **Pluggable MCP transports** — add stdio MCP alongside HTTP MCP so local
  LLM tools share the same abstraction.
- **Offline / air-gapped toggle** — important for OSS credibility.
- **(Deferred)** small Next.js or VS Code extension dashboard once Run API
  + SSE exist.

---

## 4. LLM / model layer ideas

- Add **Azure OpenAI** as a provider behind `LLMRouter` (pay-as-you-go;
  PTUs only if volume demands it).
- When multi-provider: put **APIM AI Gateway** in front for semantic
  cache, token limits, load balancing across Azure OpenAI / Claude,
  content safety.
- **Prompt + response logging to Blob** — essential for debugging / eval,
  cheap.
- **Nightly eval harness** — Postgres dataset of historical PRs / issues
  with ground truth, run against current prompts, trend quality over
  time. Cheap, massive quality dividend.
- **Azure AI Foundry hosted agents / prompt optimizer** — only if we want
  managed eval / optimization; otherwise overkill.

---

## 5. Observability and ops

- **Application Insights via OTel exporter** — minimum baseline.
- **Log Analytics workspace** shared with AKS.
- **Azure Monitor alerts**: webhook 5xx rate, LLM error rate, queue depth,
  Postgres / Redis health, Key Vault secret expiry.
- **Workbooks dashboard**: runs/day per agent, avg tool latency,
  LLM $ spent, top error classes. Nearly free once telemetry exists.
- **Correlation IDs** flow from
  `X-GitHub-Delivery → run_id → agent_id → tool_call_id → llm_call_id`
  as span attributes across every layer.

---

## 6. Security and governance

- **Managed Identity / AKS Workload Identity** for every pod — no
  long-lived secrets in env vars.
- **Private endpoints** for Postgres, Redis, Key Vault, Storage — keep the
  backend VNet-internal.
- **Azure Policy** on the resource group: deny public IPs, require HTTPS,
  require TLS 1.2+.
- **Webhook replay-window rejection** (e.g. drop deliveries >5 min old) in
  addition to the existing signature check.
- **GitHub App private-key rotation** with two active keys for
  zero-downtime cutover.
- **Row-level security / schema-per-install** once multi-tenant.
- **Content Safety / PII redaction** on outgoing PR comments and issues.

---

## 7. Opinionated "next 5" shortlist

If we could only do five things next, in order:

1. **Postgres Flex (Burstable) + `MemoryBackend` abstraction** — unblocks
   multi-replica + durable state.
2. **Azure Cache for Redis (C0)** — dedup, locks, token cache, rate-limit
   counters.
3. **App Insights + OTel end-to-end tracing** — do this *before* adding
   more distributed pieces.
4. **Async Run API + background worker** (Postgres-queue or
   Celery/Redis) — webhooks become durable; a UI becomes possible.
5. **Key Vault + Managed Identity for secrets** — removes every
   "secret in env var" footgun; prerequisite for APIM and any external
   consumers later.

Everything else (AI Search, Service Bus, APIM, Foundry, graph, Fabric,
multi-tenant isolation) can wait behind these.

---

## 8. Tier summary table

| Area               | Tier A (near term)                     | Tier B (when justified)          | Tier C (defer)                       |
| ------------------ | -------------------------------------- | -------------------------------- | ------------------------------------ |
| Relational DB      | Postgres Flex (Burstable)              | pgvector extension               | Azure SQL, Managed Instance          |
| Cache / coordination | Azure Cache for Redis Basic C0       | —                                | —                                    |
| Secrets            | Key Vault + CSI + MI                   | Cross-region replication         | HSM-backed keys                      |
| Search / memory    | —                                      | Azure AI Search Basic / pgvector | Cosmos Mongo / Graph                 |
| Object storage     | —                                      | Blob Standard LRS + lifecycle    | ADLS Gen2 hierarchical               |
| Messaging          | In-process async                       | Service Bus standard             | Event Grid custom topics             |
| Gateway            | Ingress on AKS                         | —                                | APIM AI Gateway                      |
| Observability      | App Insights via OTel                  | Workbooks, Log Analytics alerts  | ADX / Fabric / Power BI              |
| LLM providers      | Claude + Copilot (existing)            | Azure OpenAI via router          | AI Foundry hosted agents             |
| Governance         | MI + Key Vault + private endpoints     | Policy + RBAC automation         | Content Safety, PII redaction        |

---

## 9. Decision log pointers

- Tier A items are additive and preserve local-only / GitHub Actions
  operation; they do not change the phase-1 AKS recommendation in
  [`azure-mcp-architecture-plan.md`](./azure-mcp-architecture-plan.md).
- Tier B and C items are **gated on concrete need** — document that
  need in a short ADR before picking them up.
- Any item that introduces a new Azure service should first show up as
  an additive section in `MaintainerConfig` with a default-off feature
  flag.
