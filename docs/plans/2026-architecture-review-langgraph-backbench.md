# Architecture Review: LangGraph, Backbench, and Distributed Orchestration (2026-04-26)

## Executive Summary

Caretaker is architecturally ambitious and functionally sophisticated — it has
shadow-decision infrastructure, multi-executor dispatch, goal engines, and a
rich test suite. But three structural weaknesses limit how reliably it resolves
issues automatically:

1. **The maintainer-check deadlock.** `caretaker/pr-readiness` publishes a
   `failure` conclusion by default, and `MergeAuthorityConfig` — the config
   knob intended to make that advisory — was never wired into `PRAgentConfig`.
   If a consumer repo adds the check to branch protection (a natural thing to
   do), every bot-opened fix PR gets stuck immediately.

2. **No true backbench.** The entire agent pipeline runs synchronously inside a
   GitHub Actions job. Long-running or multi-pass fixes (CI flakiness, complex
   refactors) require a new cron tick to continue. There is no durable,
   resumable execution layer for these cases. The `k8s_worker` covers coding
   tasks but not the full agent pipeline.

3. **LangGraph is absent, costing durable execution.** The hand-rolled
   orchestrator and `foundry/tool_loop.py` have no checkpointing. A
   runner timeout, OOM, or transient API error mid-loop discards all
   progress. LangGraph 1.0 (GA, Oct 2025) solves this directly.

---

## Current Architecture Assessment

### Strengths

| Strength | Evidence |
|---|---|
| Shadow-decision rollout system | `@shadow_decision` decorator + `:ShadowDecision` Neo4j node + Braintrust eval harness |
| Rich PR state machine | `PRTrackingState` enum + `evaluate_pr` with CI/review/readiness evaluators |
| Three executor backends | Copilot (comment), Foundry (in-process tool loop), Claude-Code (action hand-off) |
| Causal chain tracking | `<!-- caretaker:causal id=... -->` markers on every comment |
| Prompt caching | `cache_control: ephemeral` on system blocks in both `AnthropicProvider` and `LiteLLMProvider` |
| Robust self-heal | `fix_ladder.py` + `fix_ladder_pr.py` + self-heal agent |
| Comprehensive tests | ~95 test modules, `asyncio_mode = "auto"` |

### Weaknesses (new findings beyond existing master plan)

#### W-NEW-1: MergeAuthorityConfig is unconnected (CRITICAL — causes the blocking issue)

`config.py:65` defines `MergeAuthorityConfig` with three modes
(`advisory / gate_only / gate_and_merge`) but the class is **not a field of
`PRAgentConfig`**. It was designed to cap what the readiness check can do but
was never wired up.

**Consequence:** `_publish_readiness_check` always publishes `failure` for
blocked PRs. In `advisory` mode the intent is that the check is informational
only — it should use `neutral` so GitHub branch protection can never treat it
as blocking. Until the field is connected and the conclusion remapped, the
design intent is dead code.

**Fix:** Add `merge_authority: MergeAuthorityConfig` to `PRAgentConfig` and
remap the conclusion in `_publish_readiness_check`:

```python
# advisory mode: informational only — never block branch protection
if self._config.merge_authority.mode == MergeAuthorityMode.ADVISORY:
    if check_conclusion == "failure":
        check_conclusion = "neutral"
```

#### W-NEW-2: Sequential agent registry with no backbench queue

`registry.run_all()` iterates agents in registration order, `await`ing each
one before the next starts. For the `full` mode this means PR agent, issue
agent, devops agent, security agent, etc. all run in series. Two problems:

- **No parallelism.** Agents are independent and fully async — there is no
  reason they must serialize.
- **No backbench.** A GitHub Actions job has a 6-hour hard limit and is
  synchronous end-to-end. A complex multi-pass fix (Copilot pushes → CI runs →
  caretaker re-reviews → fix requested → next cron tick) requires multiple full
  runs separated by hours. There is no mechanism to "park" a task and resume it
  when the prerequisite (CI green) is met.

#### W-NEW-3: No LangGraph — missing durable execution in the tool loop

`foundry/tool_loop.py` is a hand-rolled `while tool_calls` loop. It has no
checkpointing. A runner restart at iteration 8 of 20 loses all work. LangGraph's
`PostgresSaver` (or even `SqliteSaver`) would checkpoint after every node
execution and resume from the last successful point.

#### W-NEW-4: GitHub App as primary runtime not yet implemented

Research spike R1 is identified but un-planned. The current model (cron + event
webhooks via GitHub Actions jobs) requires a full CI runner boot (30-60 seconds)
for every event. A persistent GitHub App backend with the webhook dispatcher
already wired in (`mcp_backend/main.py`) could handle events sub-second.

---

## Gap Analysis: Why Simple Issues Don't Auto-Resolve

The user observed: *"simple issues won't get resolved bc maintainer check is
already blocking."*

This is the failure path for a bot-opened fix PR:

```
1. Issue filed (e.g. config bug, simple test failure)
2. issue_agent triages → assigns to Copilot or Foundry
3. Copilot/Foundry opens a fix PR
4. caretaker runs on the PR event
5. pr_agent evaluates readiness → blockers = ["ci_pending"]
6. _publish_readiness_check posts conclusion = "failure" (pending maps to
   in_progress, but if ANY other blocker is present it publishes failure)
7. If branch protection lists caretaker/pr-readiness as required:
      → PR is blocked by caretaker's own check
      → The PR that would fix the simple issue cannot merge
8. On next cron, same evaluation, same result
      → Issue never resolves automatically
```

Secondary path (the `action_required` problem from QA finding #7):
```
1. Bot pushes commits to PR
2. GitHub Actions shows CI as "action_required" — manual owner approval needed
3. caretaker has no API to approve these runs
4. CI never runs → readiness check stays in_progress indefinitely
```

### Root causes

| Root cause | Where | Impact |
|---|---|---|
| `MergeAuthorityConfig` never wired | `config.py` + `agent.py` | `failure` conclusion blocks branch protection |
| `conclusion="failure"` in advisory mode | `pr_agent/states.py:262`, `agent.py:1333` | check blocks PRs instead of advising |
| `action_required` runs not auto-approved | `pr_ci_approver` exists but optional | bot-pushed CIs stuck |
| No backbench to retry after CI completes | architecture gap | single-pass evaluation strands stalled fixes |

---

## LangGraph Fit Analysis

### Where LangGraph adds the most value (high ROI, low disruption)

#### 1. Replace `foundry/tool_loop.py` with a LangGraph subgraph

The tool loop is already structured as a graph: `start → call_llm → execute_tools → [loop | end]`. Migrating to LangGraph gives:

- **Durable execution** — `SqliteSaver` for single-node, `PostgresSaver` for
  multi-node. Runner restarts resume from last checkpoint.
- **Parallel tool execution** — multiple tool calls in one LLM response can fan
  out via LangGraph's `Send` API.
- **Retry policies per node** — `@node(retry=RetryPolicy(...))` covers 429s and
  5xx without custom logic.
- **Tracing** — LangSmith traces replace the current custom `tool-output` fences.

```python
# Proposed tool_loop graph (minimal LangGraph migration)
from langgraph.graph import StateGraph, END
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

builder = StateGraph(ToolLoopState)
builder.add_node("call_llm", call_llm_node)
builder.add_node("execute_tools", execute_tools_node)
builder.add_conditional_edges("call_llm", route_after_llm,
    {"tools": "execute_tools", "done": END})
builder.add_edge("execute_tools", "call_llm")
checkpointer = AsyncSqliteSaver.from_conn_string(":memory:")
graph = builder.compile(checkpointer=checkpointer)
```

#### 2. Parallelize `registry.run_all()` with `asyncio.gather` (quick win, no LangGraph required)

Agents are already async. A simple change in `registry.py` replaces sequential
dispatch with concurrent:

```python
# Current (sequential):
for agent in agents:
    result = await agent.execute(state, payload)

# Proposed (parallel):
results = await asyncio.gather(
    *[agent.execute(state, payload) for agent in agents],
    return_exceptions=True,
)
```

This alone reduces a 6-agent `full` run from ~120 s to ~30 s (agents are
I/O-bound waiting on GitHub API and LLM calls).

#### 3. Backbench queue: Redis + LangGraph Server (medium term)

The `mcp_backend` already has Redis in `docker-compose.yml`. A lightweight
backbench architecture:

```
Webhook/cron event
  → mcp_backend enqueues job (delivery_id, event_type, payload, priority)
  → Redis list / LangGraph task queue
  → Worker pool pulls jobs (1 worker per concurrent LangGraph run)
  → LangGraph run with PostgresSaver checkpointing
  → Results written back to GitHub via API
```

LangGraph Server's built-in task queue (Postgres-backed, v1.0+) is the cleanest
path: it already handles deduplication via `delivery_id` as idempotency key,
provides a REST API for job inspection, and retries failed nodes automatically.

For self-hosted deployment without the LangGraph enterprise license, the free-tier
LangGraph Server (up to 100k node executions/month) suffices for most repos.

#### 4. Supervisor pattern for multi-agent coordination (long term)

The `AgentRegistry` maps closely to the `langgraph-supervisor` pattern:

```python
# Each current agent becomes a LangGraph subgraph node
from langgraph_supervisor import create_supervisor

supervisor = create_supervisor(
    [pr_agent_graph, issue_agent_graph, devops_agent_graph],
    model=llm,
    prompt="Route maintenance tasks to the appropriate agent.",
)
```

This enables the supervisor to dynamically prioritize agents based on goal
engine output rather than running all agents every tick.

---

## Options Analysis

### Option A — Fix the immediate blockers only (1–2 weeks)

Scope: Wire `MergeAuthorityConfig`, fix conclusion mapping, add asyncio.gather
for parallel agents, document `action_required` workaround.

**Pros:** Directly fixes the user-reported blocking issue. Zero new
dependencies. Ships fast.

**Cons:** Doesn't address the durable-execution gap or backbench. Complex
multi-pass fixes still require multiple cron ticks.

### Option B — Migrate foundry tool loop to LangGraph (2–4 weeks)

Scope: Option A + replace `foundry/tool_loop.py` with LangGraph subgraph +
`SqliteSaver` checkpointing.

**Pros:** Durable execution for all coding tasks. Parallel tool calls. Better
tracing. No change to external interfaces.

**Cons:** New dependency (`langgraph`, `langgraph-checkpoint-sqlite`). Requires
migrating tests.

### Option C — Full backbench queue via LangGraph Server (4–8 weeks)

Scope: Option B + Redis-backed job queue + LangGraph Server deployment + webhook
dispatcher → queue instead of synchronous dispatch.

**Pros:** True backbench. Events processed sub-second (no runner boot time).
Resumable execution across restarts. Scales horizontally.

**Cons:** Requires Postgres + Redis in production (already in docker-compose but
not deployed). Significant architectural change to the dispatch model.

### Option D — GitHub App as primary runtime (8–12 weeks, R1 spike)

Scope: Option C + make `mcp_backend` the primary event handler (always-on
GitHub App) with GitHub Actions as fallback for consumers who can't self-host.

**Pros:** Eliminates the cron delay and runner boot overhead. Sub-second event
processing. Most powerful architecture.

**Cons:** Requires deployment (k8s or managed), persistent secrets management,
App installation management. Highest ops burden.

---

## Recommended Plan

### Phase 0 — Ship now (this PR, days)

1. **Wire `MergeAuthorityConfig` into `PRAgentConfig`** and make
   `_publish_readiness_check` use `neutral` instead of `failure` when
   `merge_authority.mode == advisory`. This unblocks the blocking-deadlock
   issue immediately.
2. **Document the advisory-vs-gate distinction** in `docs/configuration.md` so
   operators know not to add the readiness check to branch protection unless
   they explicitly want gate behavior.

### Phase 1 — Parallel agents + backbench foundations (weeks 1–2)

| Task | Effort | Impact |
|---|---|---|
| `asyncio.gather` in `registry.run_all()` | 1 day | 4× faster full runs |
| Add `backbench_queue` config section (Redis URL, worker count) | 1 day | enables Phase 2 |
| Auto-approve `action_required` runs for trusted bot actors | 2 days | unblocks Copilot CI |
| LangGraph dependency + `SqliteSaver` in FoundryExecutor | 2 days | durable tool loops |

### Phase 2 — LangGraph tool loop migration (weeks 2–4)

Replace `foundry/tool_loop.py` with a LangGraph subgraph. Preserve the existing
`LLMProvider` abstraction — LangGraph wraps it as a node rather than replacing
it. This keeps the LiteLLM multi-provider support intact.

Key design points:
- `ToolLoopState` = current loop variables (messages, iteration, token_budget,
  outcome)
- `call_llm_node` = calls `provider.complete_with_tools()`, stores response
- `execute_tools_node` = runs tool calls in parallel via `Send` API
- Checkpointer = `AsyncSqliteSaver` for single-node, `PostgresSaver` for
  LangGraph Server

### Phase 3 — Backbench queue (weeks 4–8)

Deploy LangGraph Server (free tier) as the backbench. The `mcp_backend` webhook
receiver enqueues events rather than running them synchronously. Workers pull
from the queue, run LangGraph graphs with PostgresSaver, and checkpoint after
every meaningful state transition.

Idempotency key: `X-GitHub-Delivery` header (GitHub-unique per delivery).

Duplicate-comment prevention: existing `<!-- caretaker:causal -->` markers +
the `state/dedup.py` module already in place.

### Phase 4 — Supervisor pattern (weeks 8–12)

Migrate `AgentRegistry.run_all()` to a LangGraph supervisor that reads goal
engine output to dynamically prioritize agents rather than running all of them
on every tick. The supervisor can also fan out to multiple repos in parallel
(fleet-wide maintenance).

---

## Prioritized Backlog Additions (complement existing 2026-Q2 plan)

These items are **not** in the existing `2026-Q2-agentic-migration.md` plan:

| ID | Description | Effort | Blocks |
|---|---|---|---|
| B1 | Wire `MergeAuthorityConfig` + advisory→neutral conclusion | 2h | the blocking deadlock |
| B2 | `asyncio.gather` in `registry.run_all()` | 1d | faster runs, backbench prereq |
| B3 | LangGraph + `SqliteSaver` for `foundry/tool_loop.py` | 3d | durable coding tasks |
| B4 | LangGraph Server backbench queue config + worker | 1w | true async backbench |
| B5 | `after_model` HITL middleware for high-stakes actions | 2d | safety for gate_and_merge mode |
| B6 | `interleaved-thinking-2025-05-14` header in PR reviewer | 1d | better multi-step review |
| B7 | `langgraph-supervisor` for orchestrator dispatch layer | 2w | fleet parallelism |
| B8 | GitHub App as primary runtime (always-on webhook handler) | 4w | sub-second event processing |

---

## Key Technical Invariants for LangGraph Adoption

The following constraints come from the LangGraph 1.0 production docs and are
non-negotiable for a stable deployment:

1. **Never wrap `interrupt()` in try/except** — interrupts are control-flow
   exceptions. Catching them silently kills HITL.
2. **Everything before a checkpoint must be idempotent** — nodes re-run from the
   start on resume. The `caretaker:causal` marker pattern already covers
   GitHub mutations; extend it to Neo4j writes.
3. **PostgresSaver requires `autocommit=True, row_factory=dict_row`** on the
   psycopg3 connection. Add `checkpointer.setup()` once on startup.
4. **Add a checkpoint TTL CronJob** — `PostgresSaver` does not auto-prune.
   Threads grow unbounded without a cleanup job (langgraph-js#1138).
5. **Bot identity check at the graph entry edge** — skip events where
   `actor in AUTOMATED_ACTORS` before entering the graph to prevent infinite
   loops. The existing `dispatch_guard.py` logic is the right place.
6. **Set `recursion_limit` on the graph** — default is 25. For the tool loop,
   use the existing `max_iterations` config value as the limit.
7. **Prompt cache TTL is 5 minutes (changed early 2026)** — tool loops exceeding
   5 min won't get cache hits on the system prompt. Keep loops tight or accept
   the cost; the existing 60s timeout per LLM call is safe.

---

## Summary

The biggest immediate win is **B1** (2 hours): wiring `MergeAuthorityConfig`
and remapping `failure → neutral` in advisory mode. This directly unblocks the
issue the user reported. Everything else in this document is additive.

For the backbench specifically: the system doesn't need a full LangGraph Server
deployment to get meaningful async behaviour. Even `asyncio.gather` in the
registry (B2) combined with the existing k8s_worker for coding tasks brings
significant improvement. The LangGraph Server path (B4) is the right long-term
architecture but is a 1-week project, not an emergency patch.

The existing `2026-Q2-agentic-migration.md` plan is sound. This review adds
three new categories it doesn't cover: (1) the merge-authority wiring bug,
(2) LangGraph as the backbench execution engine, and (3) the parallel agent
registry quick win.
