# GitHub App — Active-Mode Rollout Runbook

This document is the operator's step-by-step guide for promoting the
caretaker GitHub App from silent acknowledgement (`off`) → safe
observation (`shadow`) → event-driven agent execution (`active`).

For background on *why* the ladder exists see
[`docs/github-app-phase2.md`](./github-app-phase2.md).

---

## Quick reference — env vars

| Var | Values | Effect |
|---|---|---|
| `CARETAKER_WEBHOOK_DISPATCH_MODE` | `off` (default), `shadow`, `active` | Controls which code path the dispatcher takes |
| `CARETAKER_GITHUB_APP_ID` | integer | Required for active mode |
| `CARETAKER_GITHUB_APP_PRIVATE_KEY` | PEM string | Required for active mode (or use `_PATH`) |
| `CARETAKER_GITHUB_APP_PRIVATE_KEY_PATH` | file path | Alternative to inline PEM |
| `CARETAKER_GITHUB_APP_WEBHOOK_SECRET` | string | Required for signature verification |
| `CARETAKER_WEBHOOK_ACTIVE_AGENTS` | comma-separated names | Active-mode allow-list; unset = all agents run |
| `CARETAKER_DRY_RUN` | `true` / `1` | All agents skip mutating API calls |
| `CARETAKER_CONFIG_PATH` | file path | Local fallback `config.yml`; default `.github/maintainer/config.yml` |
| `REDIS_URL` | `redis://…` | Enables Redis-backed delivery dedup + token cache |

### Verify live config instantly

```sh
curl -s https://<backend-host>/health | python3 -m json.tool
```

Expected shape:

```json
{
  "status": "ok",
  "version": "0.x.y",
  "dispatch_mode": "shadow",
  "github_app_configured": "true"
}
```

---

## Stage 0 — Prerequisites

Before starting the ladder, confirm:

- [ ] GitHub App is installed on the target organisations/repos
- [ ] `CARETAKER_GITHUB_APP_ID`, `CARETAKER_GITHUB_APP_PRIVATE_KEY`,
      and `CARETAKER_GITHUB_APP_WEBHOOK_SECRET` are set in the backend
      deployment (staging first, production later)
- [ ] `REDIS_URL` is set so delivery dedup is durable across replicas
- [ ] Prometheus / Grafana dashboards are accessible:
  - `caretaker_webhook_events_total{mode, event, outcome}`
  - `worker_jobs_total{job="webhook:<agent>", outcome}`
  - `caretaker_errors_total{kind="webhook_dispatch"}`
- [ ] You can tail backend pod logs and filter by `delivery_id`

---

## Stage 1 — Shadow mode on staging

**Goal:** observe real webhook traffic with zero agent execution risk.

### 1.1 Deploy

Set on the staging backend:

```
CARETAKER_WEBHOOK_DISPATCH_MODE=shadow
```

Leave all other dispatch vars unset (no `CARETAKER_WEBHOOK_ACTIVE_AGENTS`).

### 1.2 Verify

```sh
curl -s https://<staging-host>/health
# → "dispatch_mode": "shadow"
```

Send a test webhook (or open/close a PR on a connected repo) and confirm:

```sh
# Prometheus — events should appear within 60 s
caretaker_webhook_events_total{mode="shadow"}

# Logs — one line per would-be agent per delivery
grep "would-dispatch" <pod-logs>
```

### 1.3 Observe for at least 48 hours

Answer the following before proceeding:

| Question | Acceptable answer |
|---|---|
| Are webhook events arriving? | `caretaker_webhook_events_total > 0` |
| Is fan-out (agents per event) as expected? | Matches `EVENT_AGENT_MAP` in `github_app/events.py` |
| Are there unknown/unexpected event types? | If yes, add them to the map or ignore intentionally |
| Is `caretaker_errors_total{kind="webhook_dispatch"}` zero? | Yes |
| Do log lines carry `delivery_id`, `installation_id`, `repository`? | Yes |

---

## Stage 2 — First active agent: `pr-reviewer`

**Goal:** run a single low-risk agent in production to validate the
full dispatch path (token mint → context build → agent execute → metric).

### 2.1 Deploy

```
CARETAKER_WEBHOOK_DISPATCH_MODE=active
CARETAKER_WEBHOOK_ACTIVE_AGENTS=pr-reviewer
CARETAKER_DRY_RUN=false
```

`pr-reviewer` is chosen first because:
- It is read-mostly (posts review comments; no merges, no branch mutations)
- It has the highest value-to-risk ratio for early validation
- It runs on `pull_request` events which fire frequently on active repos

### 2.2 Confirm active dispatch

```sh
curl -s https://<host>/health
# → "dispatch_mode": "active"

# Prometheus — active outcome should appear
caretaker_webhook_events_total{mode="active", outcome="active"}

# Per-agent success rate
worker_jobs_total{job="webhook:pr-reviewer", outcome="success"}

# Errors must stay at zero
caretaker_errors_total{kind="webhook_dispatch"}
```

### 2.3 Watch for 24 hours

| Metric | Target |
|---|---|
| `worker_jobs_total{job="webhook:pr-reviewer", outcome="success"}` | >0 |
| `worker_job_duration_seconds{job="webhook:pr-reviewer"} p95` | <60 s |
| `caretaker_errors_total{kind="webhook_dispatch"}` | 0 |
| `caretaker_webhook_events_total{outcome="active_partial"}` | 0 (no per-agent failures) |

If `active_partial` appears, grep pod logs for `agent=pr-reviewer` to
see the failure reason before expanding.

---

## Stage 3 — Expand the allow-list

Promote agents one at a time. Suggested order (by risk ascending):

| Step | Agent added | Trigger event | Risk notes |
|---|---|---|---|
| 3a | `pr` | `pull_request`, `check_run`, `workflow_run` | Labels, comments, merge-authority decisions |
| 3b | `devops` | `workflow_run` | Creates GitHub Issues |
| 3c | `self-heal` | `workflow_run` | Creates GitHub Issues |
| 3d | `issue` | `issues`, `issue_comment` | Triage comments, closes issues |
| 3e | `security` | `dependabot_alert`, `code_scanning_alert` | Creates Issues; high-value catches |
| 3f | `docs` | `push` | Opens PRs against doc files |

For each step:

```
CARETAKER_WEBHOOK_ACTIVE_AGENTS=pr-reviewer,pr   # example: 3a
```

Redeploy and watch the same metrics table from Stage 2 for **each new
agent** for at least 24 hours before adding the next.

---

## Stage 4 — Remove the allow-list (all agents)

Once every agent in Stage 3 has been individually validated:

```
CARETAKER_WEBHOOK_DISPATCH_MODE=active
# Remove CARETAKER_WEBHOOK_ACTIVE_AGENTS entirely
```

Verify the health endpoint no longer shows partial dispatch and that
`caretaker_webhook_events_total{outcome="active"}` is the dominant
outcome (not `active_partial`).

---

## Stage 5 — Promote to production

Repeat Stages 1–4 on production, starting with `shadow` first.

Production-specific checklist:

- [ ] Redis is configured and healthy (`REDIS_URL` set, reachable)
- [ ] GitHub App installation covers all production repos
- [ ] `CARETAKER_DRY_RUN` is **not** set (or set to `false`)
- [ ] Alerting is wired on `caretaker_errors_total{kind="webhook_dispatch"} > 0`
- [ ] On-call runbook references this document

---

## Stage 6 — Deprecate the CLI orchestrator

Once production active dispatch has been stable for **2 weeks**:

1. Confirm no consumer repos are relying on the CLI-only agents
   (check `AGENT_MODES` for agents not yet in `EVENT_AGENT_MAP`)
2. Open a deprecation PR that:
   - Adds remaining agents to `github_app/events.py` `EVENT_AGENT_MAP`
   - Marks the CLI orchestrator `caretaker run` as deprecated in help text
   - Updates docs to point to the App as the primary runtime
3. Give consumer repos a 2-sprint migration window

---

## Rollback procedure

At any stage, rollback is a single env-var change:

```sh
# Immediately safe — all agents stop executing; existing deliveries
# that are mid-flight finish but no new ones dispatch.
CARETAKER_WEBHOOK_DISPATCH_MODE=shadow

# Or full stop:
CARETAKER_WEBHOOK_DISPATCH_MODE=off
```

No database migration, no data loss. The dispatcher state is purely
in-memory (rebuilt from env vars on first webhook after restart).

---

## Incident triage

| Symptom | First check |
|---|---|
| Webhook returns 401 | Signature secret mismatch — check `CARETAKER_GITHUB_APP_WEBHOOK_SECRET` |
| Webhook returns 503 | Secret env var not set |
| `outcome=error` on every dispatch | `CARETAKER_GITHUB_APP_ID` or private key wrong; check token broker logs |
| `outcome=active_partial` | One agent failed — grep `agent=<name> delivery=<id>` in logs |
| `outcome=timeout` | Agent exceeded 120 s; check LLM latency or GitHub API slowness |
| No metrics at all | `CARETAKER_METRICS_PORT` wrong, or scrape config missing |

To follow a single delivery end-to-end:

```sh
kubectl logs -l app=caretaker-backend --since=1h | grep "delivery=<id>"
```
