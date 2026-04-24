# GitHub App — Phase 2 rollout

This is the execution notes for Phase 2 of
[`docs/github-app-plan.md`](./github-app-plan.md): turning the webhook
receiver from a silent acknowledgement into a real event-driven agent
dispatcher.

Phase 1 shipped signature verification, delivery dedup, installation
token minting, and the event → agent map. Phase 2 closes the loop.

## Why shadow-first

Jumping directly from "no dispatch" to "real agent execution in
production" would change two things at once: the dispatcher plumbing
and the per-event semantics. If we then saw a spike in agent errors,
it would be ambiguous whether the fault was in the dispatcher or in
the agents themselves.

Shadow mode fixes that. The dispatcher is live and making real
decisions about which agents *would* run, but nothing is actually
invoked. Every event that comes in is logged with `delivery_id`,
`installation_id`, `repository`, and the resolved agent list, and
emits a `caretaker_webhook_events_total{mode="shadow"}` sample plus a
`worker_jobs_total{outcome="shadow"}` sample per fan-out agent.

After a week of shadow traffic we have answers to:

- Which event types actually fire on our installations, and at what
  rate?
- Is the fan-out (one event → N agents) what we expect, or are we
  over-subscribed?
- Are there installations we've forgotten about? (Silent heartbeat.)
- Do the logs look right — correlation id, structured fields, level?

Only then does flipping to `active` mode change behaviour.

## Modes

Set via `CARETAKER_WEBHOOK_DISPATCH_MODE` on the backend:

| Mode | Behaviour |
|---|---|
| `off` (default) | Webhook endpoint is a plain 202-acking recorder. Matches Phase 1 exactly. |
| `shadow` | Dispatcher resolves agents, emits metrics + structured logs, does **not** run any agent. Safe everywhere. |
| `active` | Dispatcher runs resolved agents. **Not wired yet** — raises `NotImplementedError` (which the dispatcher catches and records as `outcome=error`). Follow-up PR lands this. |

Unknown values downgrade to `off` with a warning log — never crash the
webhook handler over a typo in an env var.

## Resilience properties

The dispatcher is designed around "the webhook handler must not go
down":

- **Bounded wall clock.** Signature + dedup + enqueue all run inline
  on the handler thread; dispatch itself runs as a background
  asyncio task so GitHub always sees a fast 202.
- **Crash-proof dispatch.** `WebhookDispatcher.dispatch` catches any
  exception, records `caretaker_errors_total{kind="webhook_dispatch"}`,
  and returns a well-formed `DispatchResult` with `outcome="error"`.
  One broken agent in a fan-out never breaks the others.
- **Dedup survives replicas.** Uses the existing Redis-backed
  `build_dedup()` so if GitHub retries a delivery against a different
  replica, we still skip it.
- **Correlation.** Every log line carries `delivery_id`. Tailing
  `delivery=<id>` in the backend pod logs reconstructs the full
  fan-out for any GitHub delivery.

## Metrics you should be watching

| Metric | Question it answers |
|---|---|
| `caretaker_webhook_events_total{mode, event, outcome}` | Are we receiving webhooks? Is the mode what I expect? |
| `worker_jobs_total{job="webhook:<agent>", outcome}` | Per-agent fan-out rate, and eventually per-agent success rate. |
| `worker_job_duration_seconds{job="webhook:<agent>"}` | Agent latency (populated in active mode). |
| `caretaker_errors_total{kind="webhook_dispatch"}` | Dispatcher crash rate — should sit at zero. |

## What's still out of scope here

Deliberate cuts so this PR stays small:

1. **Per-installation `AgentContext` construction.** Active mode needs
   it (installation token → `GitHubClient`, `.github/maintainer/config.yml`
   fetched via Contents API, memory store opened against the shared
   backend). Separate PR.
2. **Agent `event_payload` handling.** The base protocol already
   accepts `event_payload` but no agent currently uses it — that
   migration is per-agent and independent of the dispatcher.
3. **Redis-backed job queue.** Today we use `asyncio.create_task` on
   the backend process. Multi-replica durability can wait until we
   have real traffic to tune queue depth and visibility timeout
   against.
4. **OAuth user-to-server tokens.** Still flow through the existing
   `COPILOT_PAT` path until the Copilot assignment refactor.
5. **Deprecating `maintainer.yml`.** Consumer repos keep running the
   CLI orchestrator until active mode has been live and healthy for
   long enough to switch primary over.

## Rollout steps

1. **Ship Phase 2 dispatcher PR (this one).** Default mode stays
   `off`; no behaviour change.
2. **Flip `CARETAKER_WEBHOOK_DISPATCH_MODE=shadow` on staging.**
   Point a real GitHub App install at it and let real deliveries
   flow. Watch metrics + logs for a day.
3. **Flip staging to `shadow` for a week.** Read the dashboards. Add
   a test install that deliberately fires every event type in the
   map to verify fan-out matches the table.
4. **Build active mode (follow-up PR).** Per-installation context
   factory, one agent at a time, behind an allow-list env var so we
   can go agent-by-agent.
5. **Flip one agent to `active`** (probably `pr-reviewer`, which is
   the pain point that motivated this work). Rest stay in shadow
   until each one is validated.
6. **Expand to all agents; then deprecate the CLI orchestrator.**
