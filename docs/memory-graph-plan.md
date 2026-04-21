# Memory graph + enhanced memory — design

Plan for turning caretaker's existing graph backend from a dashboard
replica into a **queryable memory substrate** that agents read from
and write to during a run, and to plug fleet-level knowledge into the
same surface.

Inputs:

- Internal audit of `src/caretaker/{graph,state,evolution,causal_chain,fleet,admin}` + `frontend/src/pages/Graph.tsx`.
- External research pass on agentic-memory taxonomies, graph-based
  memory, multi-tenant RAG, forgetting/compaction, SWE-agent memory,
  OpenTelemetry GenAI spans, and agent-memory UI patterns
  (Apr 2026 snapshot).

---

## 1. State of the art (why we're changing anything)

**What's already in the repo**

| Surface | Location | Status |
|---|---|---|
| Neo4j graph | `src/caretaker/graph/{store,builder,models}.py` | Schema solid (9 node types, 13 edges). **Populated only by the admin 60-second refresh loop** — agents do not write. |
| Causal chain | `src/caretaker/causal_chain.py`, `admin/causal_store.py` | Markers embedded in issue/PR/comment bodies; in-memory index. Scanned on admin refresh. Not linked to Run / Skill nodes. |
| Memory store | `src/caretaker/state/memory.py` | SQLite KV, per-agent namespace, per-namespace 1000-entry cap, TTL support. No cross-namespace query API. |
| Skills / evolution | `src/caretaker/evolution/insight_store.py` | Confidence-scored skills (success/fail counts), mutations (parameter experiments). No link back to the causal chains that validated them. |
| Fleet registry | `src/caretaker/fleet/` | Per-repo heartbeat store (in-memory). Not a graph citizen yet. |
| Admin graph UI | `frontend/src/pages/Graph.tsx` | 2D/3D view, type filters. Reads `/api/graph/{stats,subgraph,neighbors,path,pr/*/lifecycle}`. |
| Graph sync trigger | `admin/state_loader.py:82` | `GraphBuilder.full_sync(state, causal_store=...)` every ~60 s. |

**What's missing (from the audit)**

- No agent writes to the graph — every edge is rediscovered each cycle.
- No `PR→Issue`, `Run→Agent`, `Skill→CausalEvent`, `Run→Goal`,
  `PR→Executor`, `Issue→PR(RESOLVED_BY)` edges.
- Mutations node type exists but is never synced.
- No bitemporal edge properties — no answer to "what did agent X know
  when PR #420 opened?"
- No cross-repo fleet nodes / edges.
- No OTel / GenAI span correlation — can't cross-reference with
  Phoenix / LangSmith if we ever wire one up.
- Memory store has no shared query API; agents can't see each other's
  memory without each knowing the namespace.

**What external research is converging on**

CoALA taxonomy (working / episodic / semantic / procedural) is the
common vocabulary (Letta, LangMem, Mem0, IBM docs). Graphiti's
bitemporal Neo4j model is the reference for a typed graph memory.
Multi-tenant RAG patterns give us a model for fleet-level sharing
without leaking episodic data. The Dec-2025 "Forgetting is not
optional" survey is the canonical argument for tiered compaction.
Anthropic's `memory` tool (Sep 2025) + OpenHands `agent-memory` skill
are the in-repo reference patterns. OpenTelemetry finalised GenAI
semantic conventions — `invoke_agent` spans — so observability is
now a portable story.

---

## 2. Memory-type mapping

Before changing code, label what we already have in the CoALA scheme.
Anchor for reviewers and future agents.

| CoALA type | Caretaker implementation today | Enhancement |
|---|---|---|
| **Working** | Implicit in each agent's in-run locals; not persisted | New `core_memory` block per agent — identity, active goal, active run_id, recent-action ring. Persisted in graph as `AgentState{run_id}` node, replaced every run. |
| **Episodic** | `Run`, `CausalEvent`, `AuditEvent`, `RunHistoryEntry` | Keep raw records <30 d; after that, distill into weekly `RunSummaryWeek` summary nodes. |
| **Semantic** | `Issue`, `PR`, `Agent`, `Goal`, `Repo` (new), `Skill`, config blocks | Add bitemporal edge properties. Add `:Comment`, `:CheckRun`, `:Executor` nodes so attribution chains survive. |
| **Procedural** | `Skill` (with `signature` + `sop_text` + confidence) | Promote to graph as `Skill(success_count, confidence, last_used_at)` + link to the causal events that validated it via `VALIDATED_BY`. |

This mapping lives in `docs/memory-graph-plan.md` so new agents and
contributors don't invent parallel vocabulary.

---

## 3. Graph enhancements

### 3.1 New / elevated node types

| Node | Exists | Populated | Change |
|---|---|---|---|
| Agent / PR / Issue / Run / Goal / Skill / AuditEvent / Mutation / CausalEvent | ✅ | Partial | Bitemporal props, more edges (below). |
| **Repo** | ❌ | ❌ | Every node gets `repo: "owner/name"` label — or attach to a dedicated `:Repo{slug}` node via `BELONGS_TO`. Enables cross-repo queries + privacy scoping. |
| **Comment** | ❌ | ❌ | `Comment{id, body_marker, author, created_at}`; edges to PR / Issue / CausalEvent. Lets us answer "which comment spawned this chain?" |
| **CheckRun** | ❌ | ❌ | `CheckRun{name, conclusion, run_id}` attached to `PR`. Needed to trace lint-fix causality. |
| **Executor** | ❌ | ❌ | `Executor{provider: copilot|foundry|claude_code}` — edges from `Run` via `HANDLED_BY`. Makes "which executor fixed the lint" a one-hop query. |
| **RunSummaryWeek** | ❌ | ❌ | Summarised rollup from distilled Run + RunHistory entries. Replaces raw after 30-day TTL. |
| **GlobalSkill** (fleet) | ❌ | ❌ | Shared distilled skills promoted across ≥N repos after abstraction. Lives outside the per-repo scope. |
| **Community** | ❌ | ❌ | GraphRAG-style rollups (PR clusters, failure classes). Created by a nightly batch job. |

### 3.2 New / missing edges

Inspired by "missing edges people would want" in the audit + GraphRAG
and Graphiti patterns:

- `(PR)-[:REFERENCES]->(Issue)` — parse `fixes #N` / `closes #N` / causal markers.
- `(Issue)-[:RESOLVED_BY]->(PR)` — reverse of `TrackedIssue.assigned_pr`.
- `(Run)-[:EXECUTED]->(Agent)` — which agents fired in which run.
- `(Agent)-[:USED]->(Skill)` — per-run usage, so confidence isn't global-only.
- `(Skill)-[:VALIDATED_BY]->(CausalEvent)` — audit trail for learned skills.
- `(Run)-[:AFFECTED]->(Goal)` — what a run was aiming at + outcome score.
- `(PR)-[:HANDLED_BY]->(Executor)` — Copilot / Foundry / Claude Code provenance.
- `(Repo)-[:FLEET_PEER]->(Repo)` — directly from fleet-registry data.
- `(Comment)-[:ON]->(PR|Issue)` + `(Comment)-[:EMITS]->(CausalEvent)`.

### 3.3 Bitemporal edges

Every edge gains three optional properties:

- `observed_at`: wall-clock when caretaker recorded it.
- `valid_from`: when the fact became true (often == `observed_at`).
- `valid_to`: when it stopped being true (null = current).

Lets us answer "what state was PR #420 in when run R executed?" in
one cypher query, in the style of Graphiti. Cheap — three extra
properties, no new indexes required.

### 3.4 Event-driven writes

Replace the every-60-second full-sync with **write-on-action**:

- Wrap the critical mutations (`StateTracker.save`, agent dispatch,
  CausalEvent emit, Skill update, Mutation accept/reject) with a tiny
  `GraphWriter.record(...)` call. The writer batches to the Neo4j
  driver asynchronously so the sync orchestrator doesn't block on
  cluster latency.
- Full-sync survives as a once-a-day reconciliation pass to heal
  drift (network partitions, missed events).

### 3.5 Query API extensions

Current endpoints (audit): `/api/graph/{stats,nodes,neighbors,path,subgraph,agents/{id}/impact,pr/{n}/lifecycle}`.

Add:

- `GET /api/graph/memory/{agent}/recent-actions?since=...` — last N
  edges written by a given agent.
- `GET /api/graph/causal/{event_id}/chain` — server-side walk.
- `GET /api/graph/causal/{event_id}/descendants`.
- `GET /api/graph/runs/{run_id}/impact` — runs → PRs/issues/agents touched.
- `GET /api/graph/skills/{skill_id}/validations` — causal events that
  validated the skill.
- `GET /api/graph/as-of?ts=...&seed=<node_id>&hops=2` — bitemporal
  subgraph snapshot.

---

## 4. Memory enhancements

### 4.1 Three-tier compaction

Directly from the forgetting-policies survey:

- **Tier 0 (raw)**: Run, AuditEvent, CausalEvent, Mutation, memory
  KV — retained 30 days. Queryable as-is.
- **Tier 1 (summarised)**: Weekly rollup node `:RunSummaryWeek`
  populated by a nightly job that aggregates Tier 0 into human- and
  LLM-readable markdown + counters. Raw rows then eligible for
  eviction.
- **Tier 2 (distilled)**: Skills. Already present. Extended with
  links to the weeks they were crystallised in (`LEARNED_IN`).

Immutable exemptions: `Goal` nodes and any node with the
`pinned: true` property are never pruned.

### 4.2 Salience scoring

New property on every `Run` / `CausalEvent`:

```python
salience = weighted_sum(
    0.3 * escalation_count,     # escalated runs are high-signal
    0.3 * unexpected_outcome,   # failed lint-fix, surprise PR closure
    0.2 * recency_decay,        # e^(-age_days/30)
    0.2 * connectivity,         # degree in causal chain
)
```

Pruning thresholds per tenant (configurable). A low-salience Run
after 30 days rolls into its week's summary + is deleted; high
salience lingers longer. Takes the Mnemosyne/MemoryBank pattern and
fits it into the existing data model.

### 4.3 Per-agent core memory

One small `:AgentCoreMemory{agent, run_id}` node per run, replaced
each run (or appended if we want history). Carries:

```yaml
identity: "pr_agent"
active_goal: "ci-health"
active_run_id: "run-2026W17-034"
active_pr: 431
recent_actions: [<edge ids last 10>]
context_tokens: 450
```

This is the Letta "core memory block" pattern without bringing in
their runtime. Agents read it at start, write at end, via the
existing `memory: MemoryStore` interface — we just add a graph
mirror.

### 4.4 MCP memory adapter

Caretaker's MCP backend already exposes tools. Add three tools:

- `memory.recent_actions(agent, since)` — graph-backed.
- `memory.causal_chain(event_id)` — walk + descendants.
- `memory.skill_sop(category, signature)` — returns `sop_text`.

These become callable from Claude Code / Cursor background agents /
the ClaudeCodeExecutor. Unifies the memory surface — the same
knowledge caretaker uses for routing, external agents use for
context.

### 4.5 Shared query API

Current memory is per-agent; nothing reads across namespaces. Add:

- `MemoryStore.query(namespace_glob, since, limit)` — pattern match.
- `MemoryStore.recent_keys(namespace, n)` — ring-buffer view.

Cheap — SQLite already indexes namespace+key.

---

## 5. Fleet / cross-repo memory

Per the multi-tenant RAG research:

### 5.1 Tenant isolation

- Every node gets a `repo` scalar property. Cypher queries scope via
  `WHERE n.repo = $repo`. Default: no cross-repo visibility in UI.
- `:Repo` nodes link to per-repo subgraphs via `BELONGS_TO`.

### 5.2 Shared tier

- `:GlobalSkill` label for procedural skills that pass **two gates**:
  1. Present in ≥ N repos (configurable; default 3).
  2. Abstraction pass: identifiers (repo slugs, author logins, file
     paths containing repo slug) stripped. A small regex + deny-list.
- Promotion workflow: `:Skill -> [:PROMOTED_TO] -> :GlobalSkill` with
  `confidence`, `repo_count`, `abstracted_at`.
- `:GlobalSkill` is read-shared across all repos on the same
  backend; can be exported (JSON) to seed a new caretaker install.

### 5.3 Fleet graph edges

From the fleet-registry heartbeat payload (already shipped):

- `(:Repo)-[:RUNS_AGENT]->(:Agent)` built from `enabled_agents`.
- `(:Repo)-[:GOAL_HEALTH {score, as_of}]->(:Goal)` from
  `last_goal_health`.
- `(:Repo)-[:HEARTBEAT_AT {at}]` self-loop or property on Repo —
  drives the "stale fleet client" signal.
- `(:Repo)-[:SHARES_SKILL]->(:GlobalSkill)` from the promotion pass.

### 5.4 Privacy

- Raw episodic data never leaves a tenant.
- `:GlobalSkill` only after the abstraction pass + per-skill human
  opt-out flag.
- Backend config `fleet.share_skills: bool` defaults `false`; promotion
  requires explicit enablement per repo.

---

## 6. Observability + provenance

Adopt OpenTelemetry GenAI semantic conventions (shipped Apr 2026):

- Every agent run emits an `invoke_agent` span. `trace_id` mirrors
  the caretaker causal-chain root id.
- CausalEvent gains `span_id`, `parent_span_id` properties so "which
  span caused this escalation" is a one-hop query.
- Ship a Phoenix sidecar in dev compose (`docker-compose.override.yml`)
  for free local trace viewing.
- Production: operators can point `OTEL_EXPORTER_OTLP_ENDPOINT` at
  any GenAI-aware backend (Phoenix, Datadog, LangSmith).

No backend lock-in — just the portable SDK.

---

## 7. UI

Current `/graph` is a 2D/3D full-subgraph view filtered by node type.
Enhancements (seed-scoped, never full-dump):

### 7.1 Timeline + subgraph split

- Left: temporal timeline of events (CausalEvent + Run) for the
  selected `Goal` / `Agent` / `PR`.
- Right: N-hop subgraph around whatever node the timeline cursor is
  on.
- Time slider (the bitemporal "as-of" query) scrubs both sides.

### 7.2 Goal-impact DAG

New `/graph/goal/{id}` view. DAG showing
`Goal → Runs → PRs/Issues → Outcomes`, scored by `run.goal_score_after -
run.goal_score_before`. Makes "which runs moved the needle" obvious.

### 7.3 Causal chain explorer

New `/graph/causal/{event_id}` view: root-first chain on the left, a
BFS of descendants on the right, with links into the underlying issue
/ PR / comment.

### 7.4 Memory browser

Extend `/memory`:
- Agent selector (not just namespace).
- Core-memory view (current `:AgentCoreMemory` node pretty-printed).
- Recent actions list (edges the agent wrote in the last N hours).
- Skill drilldown (clicking a skill shows the causal events it
  learned from + the per-run successes/failures).

### 7.5 Fleet overlay

New `/fleet-graph` — a macro view where each node is a repo,
size ≈ goal_health, edges = shared `:GlobalSkill`. One-screen health
check across the fleet.

---

## 8. Implementation milestones

Sized conservatively; each is ≤ 1 week of focused work.

| # | Milestone | Notes |
|---|---|---|
| **M1** | Event-driven graph writer | Wrap `StateTracker.save`, agent dispatch, causal emit, skill update. Keeps full-sync as daily reconciliation. Unit tests on the writer's async batching. |
| **M2** | Missing edges + bitemporal props | `PR→Issue`, `Run→Agent`, `Skill→CausalEvent`, `Run→Goal`, `PR→Executor`, `Issue→PR`. Adds three edge props everywhere. |
| **M3** | New node types | `:Repo`, `:Comment`, `:CheckRun`, `:Executor`. Each gets a tiny sync helper + index. |
| **M4** | Tiered compaction + salience | Nightly job: tier-0 → tier-1 rollup, tier-1 → tier-2 skills. Salience scorer + prune pass. Metrics emitted. |
| **M5** | Agent core memory + MCP memory adapter | `:AgentCoreMemory` write on run boundary. MCP tools: `memory.recent_actions`, `memory.causal_chain`, `memory.skill_sop`. |
| **M6** | Fleet graph + `:GlobalSkill` promotion | Heartbeat → Repo nodes. Promotion gate + abstraction pass. `fleet.share_skills` config flag. |
| **M7** | UI v2 | Timeline+subgraph split, Goal-impact DAG, causal explorer, memory browser, fleet overlay. Uses existing Neo4j query endpoints + the new ones from §3.5. |
| **M8** | OTel GenAI instrumentation | Spans from agents + executors. CausalEvent ↔ span_id join. Phoenix in dev compose. |

Strong recommendation: land **M1 + M2** first. They turn the graph
from "dashboard replica" into "the system of record for
post-hoc-debugging" without touching the UI. M3–M8 then layer on top
without forcing another migration.

---

## 9. Non-goals

- Replacing SQLite `MemoryStore` with the graph. KV is the right
  primitive for agent scratchpad state; graph is the right primitive
  for entity relationships. Both stay.
- Full-text search / vector embeddings in Neo4j. Neo4j has vector
  indexes as of 5.x, but we'd rather wire an embedding store only
  once we have a proven retrieval bottleneck.
- Multi-cluster Neo4j. Per-tenant data lives in one Neo4j; isolation
  is by `repo` property, not by database.
- Neo4j as the OTel backend. It's a join target, not a trace store.

---

## 10. Risks + mitigations

| Risk | Mitigation |
|---|---|
| Agent hot-path latency from graph writes | Batch + async; agents don't await the write; daily reconciliation catches drops. |
| Cross-repo leak via `:GlobalSkill` | Two-gate promotion + abstraction pass + explicit opt-in. Audit log on every promotion. |
| Neo4j cost explosion | Tiered compaction; TTL on `AuditEvent`; cap `RunHistory`. Alerting on node count per `:Repo`. |
| UI feature creep | Ship timeline+subgraph split first; every new view must seed-scope (no full dumps). |
| Compaction deletes something we wanted | Pin `Goal` + `Skill` + `PR` nodes; only `Run` + `AuditEvent` + `CausalEvent` are candidates; audit log on deletion. |
| Breaking existing admin refresh | Keep it alive as a fallback until M1 ships and event-driven writes are verified in prod for ≥ 7 days. |

---

## 11. References

Internal:

- `src/caretaker/graph/` — current graph backend.
- `src/caretaker/state/memory.py` — KV memory.
- `src/caretaker/evolution/insight_store.py` — skills.
- `src/caretaker/causal_chain.py` + `admin/causal_store.py` — causal.
- `src/caretaker/fleet/` — fleet registry.
- `docs/custom-coding-agent-plan.md` — executor routing, upstream.
- `docs/fleet-registry.md` — fleet heartbeat surface.

External (Apr 2026 snapshot):

- CoALA / LangMem taxonomy: <https://blog.langchain.com/langmem-sdk-launch/>
- Letta ADE (core-memory block pattern): <https://docs.letta.com/guides/ade/overview/>
- Anthropic `memory` tool: <https://docs.claude.com/en/docs/agents-and-tools/tool-use/memory-tool>
- Graphiti bitemporal Neo4j memory: <https://neo4j.com/blog/developer/graphiti-knowledge-graph-memory/>
- Zep bitemporal paper: <https://arxiv.org/abs/2501.13956>
- Microsoft GraphRAG / LazyGraphRAG: <https://microsoft.github.io/graphrag/>
- Cognee benchmarks: <https://www.cognee.ai/blog/deep-dives/ai-memory-evals-0825>
- Azure multi-tenant RAG: <https://learn.microsoft.com/en-us/azure/architecture/ai-ml/guide/secure-multitenant-rag>
- Federated RAG privacy survey: <https://www.emergentmind.com/topics/privacy-preserving-strategies-for-rag-systems>
- OpenHands `agent-memory` skill: <https://playbooks.com/skills/openhands/skills/agent-memory>
- "Forgetting is not optional" Dec 2025: <https://arxiv.org/html/2512.12856v1>
- OpenTelemetry GenAI agent spans: <https://opentelemetry.io/docs/specs/semconv/gen-ai/gen-ai-agent-spans/>
- Arize Phoenix (OSS OTel agent observability): <https://github.com/Arize-ai/phoenix>
- Graph-based agent memory survey: <https://shibuiyusuke.medium.com/graph-based-agent-memory-a-complete-guide-to-structure-retrieval-and-evolution-6f91637ad078>
