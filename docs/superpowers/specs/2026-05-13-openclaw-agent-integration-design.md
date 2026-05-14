# openclaw Agent Integration Design

**Date:** 2026-05-13  
**Status:** Approved  
**Author:** Ian Lintner

---

## Overview

Integrate the in-cluster openclaw server into caretaker's PR review, auto-fix, and pre-escalation pipeline. openclaw exposes an OpenAI-compatible REST API (`/v1/chat/completions`, SSE streaming) and is already running inside the Kubernetes cluster. This document covers three new units: a review backend, an inline coding agent, and a pre-escalation rung in the fix loop — preceded by a mandatory in-cluster validation phase before any code is written.

---

## Phase 0 — In-Cluster Validation (before writing code)

All implementation work is gated on confirming openclaw is reachable and responding correctly from inside the cluster. These steps are run manually against the live cluster before any PR is opened.

### 0.1 Locate the openclaw service

```bash
# List all services across namespaces, filter for openclaw
kubectl get svc -A | grep openclaw

# If namespace is unknown, check all pods too
kubectl get pods -A | grep openclaw
```

Expected: one or more services with a ClusterIP (or LoadBalancer) in a namespace such as `openclaw` or `default`. Record the namespace, service name, and port.

### 0.2 Inspect the service and pod health

```bash
# Confirm endpoints are registered (pod is Ready)
kubectl get endpoints -n <namespace> <service-name>

# Check pod status
kubectl get pods -n <namespace> -l app=openclaw

# Tail recent logs to confirm the server is serving
kubectl logs -n <namespace> -l app=openclaw --tail=50
```

Expected: at least one ready endpoint IP, pod in `Running` state, logs showing HTTP listener started.

### 0.3 Port-forward and probe the REST API

```bash
# Forward local 8080 → cluster service port (typically 8080 or 3000)
kubectl port-forward -n <namespace> svc/<service-name> 8080:<cluster-port>
```

In a second terminal, validate the OpenAI-compatible endpoint:

```bash
# Health / root check
curl -s http://localhost:8080/ | jq .

# Chat completions smoke test (streaming)
curl -s http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "openclaw/default",
    "messages": [{"role": "user", "content": "Reply with the single word: pong"}],
    "stream": true,
    "max_tokens": 10
  }' | head -20
```

Expected: SSE stream of `data: {"choices": [{"delta": {...}}]}` lines ending with `data: [DONE]`.

### 0.4 Confirm tools/invoke endpoint (optional but useful)

```bash
curl -s -X POST http://localhost:8080/tools/invoke \
  -H "Content-Type: application/json" \
  -d '{"tool": "echo", "args": {"text": "hello"}}' | jq .
```

Expected: `{"ok": true, "result": ...}` or a 404 if the tool is not registered — either is acceptable at this stage; we just need to confirm the endpoint exists.

### 0.5 Record findings

Before implementation begins, open a short note (or PR description section) capturing:

| Item | Value |
|------|-------|
| Namespace | e.g. `openclaw` |
| Service name | e.g. `openclaw` |
| Cluster port | e.g. `8080` |
| In-cluster base URL | e.g. `http://openclaw.openclaw.svc.cluster.local:8080` |
| Auth required? | yes (Bearer) / no (open private ingress) |
| Confirmed model name | e.g. `openclaw/default` |
| Streaming works? | yes / no |

These values are what get wired into `OpenclaWHttpConfig` in config.py. Do not hardcode them — they go into the caretaker YAML config and AKV secrets if auth is required.

### 0.6 Gate

**If any of the above steps fail, stop.** Fix the cluster-side issue (service not running, wrong port, networking policy blocking caretaker's namespace) before writing integration code. A broken target means tests will be mocked against the wrong assumptions.

---

## Architecture

Three new source units, two surgical edits:

```
src/caretaker/pr_reviewer/backends/openclaw_http.py   ← new review backend
src/caretaker/coding_agents/openclaw_agent.py          ← new inline coding agent
src/caretaker/config.py                                ← OpenclaWHttpConfig + pre_escalation_agent field
src/caretaker/pr_reviewer/auto_fix.py                  ← pre-escalation rung
src/caretaker/orchestrator.py                          ← register OpenclaWAgent in CodingAgentRegistry
```

### End-to-end data flow

```
Webhook → PR review
  → openclaw_http backend (REST+SSE) → ReviewResult
      ↓ REQUEST_CHANGES
  → auto_fix loop (claude_code_local / opencode_local, max_attempts)
      ↓ attempts exhausted
  → openclaw pre-escalation rung (REST+SSE, full failure history)
      ↓ cannot_fix / error
  → EscalationAgent (human digest issue)
```

---

## Component 1 — Review Backend: `openclaw_http`

**File:** `src/caretaker/pr_reviewer/backends/openclaw_http.py`

Mirrors the shape of `opencode_local.py` but replaces the subprocess with an `httpx.AsyncClient` call.

**Steps:**

1. Receive PR URL + diff text (same inputs as every other backend).
2. Build a system prompt (reuses the existing inline-reviewer prompt template).
3. POST to `{base_url}/v1/chat/completions` with `stream=true`, Bearer auth header.
4. Consume SSE `data:` lines, accumulate assistant text deltas.
5. Parse the final text for a `<!-- caretaker-review -->` JSON block.
6. Return `ReviewResult` — identical shape to every other backend.

**Config key:** `pr_reviewer.caretaker_owned_reviewer = "openclaw_http"` (alongside `"opencode_local"`).

**Error handling:**
- HTTP 4xx/5xx → raise `OpenclawHttpError` → caller catches as backend failure, falls back to next reviewer in chain.
- Timeout → same.
- No JSON block found → `ReviewResult` with a single `InlineReviewComment` noting parse failure; not a hard crash.

---

## Component 2 — Inline Coding Agent: `OpenclaWAgent`

**File:** `src/caretaker/coding_agents/openclaw_agent.py`

Implements the `CodingAgent` protocol (`mode="inline"`). Registered in `CodingAgentRegistry` under the name `"openclaw"`.

**Steps:**

1. Receive `CodingTask` (instructions, error_output, context) + `PullRequest`.
2. Clone PR head into a temp workdir (reuses `prepare_workdir` from `backends._workdir`).
3. POST to `{base_url}/v1/chat/completions` with a fix-mode prompt. Prompt includes:
   - Task instructions and error output.
   - Repo diff / file list.
   - Explicit instruction to reply with a `caretaker-fix` JSON block: `{"status": "fixed"|"cannot_fix", "patch": "...unified diff..."}`.
4. Accumulate SSE response, parse the `caretaker-fix` block.
5. `"fixed"` → apply patch via `git apply`, commit, push; return `ExecutorOutcome.COMPLETED`.
6. `"cannot_fix"` → return `ExecutorOutcome.ESCALATED`.
7. Parse/apply failure → return `ExecutorOutcome.FAILED`.
8. Cleanup workdir (same policy as claude_code_local).

**max_attempts:** Configured to `1` when used as the pre-escalation rung (one shot, not a loop).

---

## Component 3 — Pre-Escalation Rung in the Fix Loop

**File:** `src/caretaker/pr_reviewer/auto_fix.py`

A new `_run_pre_escalation_agent()` async function called by the fix dispatcher after `max_attempts` is exhausted by the primary fixers, before `EscalationAgent` posts the human digest.

**Logic:**

```
primary fixers exhausted (auto_fix.max_attempts hit)
  → config.auto_fix.pre_escalation_agent == "openclaw"?
      no  → EscalationAgent (human digest) [existing path]
      yes → _run_pre_escalation_agent(pr, all_prior_error_outputs)
                success (COMPLETED) → push commit, rearm loop, return
                ESCALATED/FAILED    → EscalationAgent (human digest)
```

The pre-escalation call passes the **full failure history** (all prior error_outputs concatenated, attempt count) so openclaw has maximum context when deciding whether it can fix or must defer.

**Config flag:** `auto_fix.pre_escalation_agent: "openclaw"` (default `""` = disabled, opt-in).

---

## Config Shape

```yaml
pr_reviewer:
  caretaker_owned_reviewer: "openclaw_http"   # or keep "opencode_local"
  openclaw_http:
    enabled: true
    base_url: "http://openclaw.openclaw.svc.cluster.local:8080"
    api_key: ""            # empty = open auth (private in-cluster ingress)
    model: "openclaw/default"
    timeout_seconds: 300
    keep_workdir_on_failure: false

executor:
  agents:
    openclaw:
      enabled: true
      max_attempts: 1      # pre-escalation rung gets exactly one shot

auto_fix:
  pre_escalation_agent: "openclaw"   # "" to disable
```

**Secret handling:** If `api_key` is non-empty it must come from `caretaker-secrets` (same AKV pattern as `OPENROUTER_API_KEY`). In-cluster private ingress without auth is preferred to avoid secret rotation overhead.

---

## New Config Models (config.py)

```python
class OpenclaWHttpConfig(StrictBaseModel):
    enabled: bool = False
    base_url: str = ""
    api_key: str = ""
    model: str = "openclaw/default"
    timeout_seconds: int = 300
    keep_workdir_on_failure: bool = False

# Added to PRReviewerConfig:
openclaw_http: OpenclaWHttpConfig = Field(default_factory=OpenclaWHttpConfig)

# Added to AutoFixConfig:
pre_escalation_agent: str = ""   # registry name of agent to try before human escalation
```

---

## SSE Streaming Pattern

openclaw's SSE response follows the standard OpenAI shape:

```
data: {"id":"...","choices":[{"delta":{"content":"chunk"},"index":0}]}
data: {"id":"...","choices":[{"delta":{"content":" more"},"index":0}]}
data: [DONE]
```

The backend accumulates `delta.content` strings into a full response buffer, then runs the `caretaker-review` / `caretaker-fix` JSON block regex over the complete text. This mirrors how `opencode_local` processes stdout line-by-line today.

---

## Testing

| Test file | What it covers |
|-----------|----------------|
| `tests/test_pr_reviewer_openclaw_http_backend.py` | Happy path (SSE → ReviewResult), timeout → backend failure, no JSON block → parse error ReviewResult |
| `tests/test_coding_agents/test_openclaw_agent.py` | `"fixed"` patch path, `"cannot_fix"` → ESCALATED, HTTP error → FAILED |
| `tests/test_pr_reviewer_auto_fix.py` | Pre-escalation rung fires after max_attempts; success short-circuits human digest; ESCALATED falls through to digest; disabled (`pre_escalation_agent=""`) skips rung entirely |

Mocking strategy: `httpx.MockTransport` / `respx` for HTTP; existing `prepare_workdir` / `cleanup_workdir` fixtures for workdir lifecycle.

---

## Out of Scope

- gRPC transport (openclaw exposes REST; no proto definitions available).
- `POST /tools/invoke` surface (reserved for a future MCP tool integration if needed).
- Complexity-classifier fast-lane (user confirmed: escalation rung only, after fix loop exhausts retries).
- Multi-tenant / K8s Job per review (existing note in opencode_local covers this future path).

---

## Open Questions (resolved)

| Question | Answer |
|----------|--------|
| Transport | REST + SSE (OpenAI-compatible) via httpx |
| Auth | Bearer token if required; open auth preferred for in-cluster private ingress |
| Escalation trigger | After fix loop exhausts max_attempts, before human digest |
| Complexity fast-lane | Out of scope |
| gRPC | Out of scope |
