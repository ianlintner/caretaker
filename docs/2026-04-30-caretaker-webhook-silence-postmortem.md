# Post-mortem: caretaker webhook silence (2026-04-30)

**Severity:** Major (caretaker fleet-wide silent for ~3 hours, no merge automation, no PR reactions)
**Detected at:** 2026-04-30 ~20:13 UTC, by operator noticing `caretaker:merge` label on PR #660 not triggering an auto-merge
**Mitigated at:** 2026-04-30 23:39 UTC, by `kubectl rollout restart` of the `caretaker-mcp` deployment with `PYTHONUNBUFFERED=1` env added
**Total observable downtime:** ~3 hours 19 minutes

---

## TL;DR

Caretaker's deployed MCP backend silently stopped reacting to GitHub webhooks. Symptoms looked like a single-cause issue (stuck rate-limit cooldown, fixed by PR #659 earlier the same day) but the post-fix redeploy uncovered three independent layered problems, two of which were silent observability failures that prevented diagnosis. The immediate symptom was resolved by rolling the pods. The underlying issues — no readiness probes, fully buffered Python stdout, an unreachable self-heal path during cooldown — remain in the codebase and need follow-up PRs before this can recur.

## Timeline (UTC, 2026-04-30)

| Time | Event |
|------|-------|
| 12:33 | PR #657 merges (`feat(pr_agent): orchestrator self-chaining for internal-only transitions`) |
| 12:40 | PR #659 merges (`fix(rate_limit): self-heal stale cooldown when GitHub bucket is healthy`) — addressed a known production incident where caretaker held a 58-minute cooldown while GitHub bucket was healthy. **This fix was on `main` but not yet deployed** |
| 16:58 | Caretaker bot reacts to PR #660 open: posts caretaker:status + pr-reviewer-handoff comments |
| 16:59 | claude-code-action review posts on PR #660 (REQUEST_CHANGES with 8 issues) |
| 17:32 | Operator pushes review-fix commit `f75f619` to PR #660 (still using deployed image from 04-29) |
| 20:19 | PR #660 merged manually (operator) |
| 20:20 | Caretaker bot last visible activity (formal review on PR #658, fleet-wide last event) |
| 20:22 | PR #658 merged manually (operator). **No caretaker reaction.** |
| 20:13–21:43 | ~90 minutes of zero caretaker activity. Operator adds `caretaker:merge` label to PR #660 — no reaction |
| 21:43 | Operator triggers `deploy-mcp.yml` workflow_dispatch on `main` (image: `sha-35299db`) |
| 21:44:55 | New pods start. **Lifespan startup hangs for ~12 minutes** (PYTHONUNBUFFERED not set, no log output) |
| 21:45:19 | Deploy job reports SUCCESS (K8s manifest applied; pods marked Ready because no readiness probes defined) |
| 22:43 | Caretaker bot makes one PR/issue interaction on a different repo (`Example-React-AI-Chat-App`) — partial recovery |
| 23:30+ | Operator finds caretaker still silent; investigates K8s state directly |
| 23:30–23:39 | Diagnosis: pods are running but cooldown engaged; webhook handler skips dispatch on every event with `outcome=deferred_cooldown` |
| 23:39 | `kubectl set env deployment PYTHONUNBUFFERED=1` triggers rollout restart |
| 23:40 | New pods serving with `cooldown=0`, `rate_limit_remaining` healthy |
| 23:42 | Test webhook event (label toggle) verifies dispatch path: `outcome=active`. **Mitigated.** |

## What happened

The deployed MCP backend stopped processing GitHub webhooks at ~20:13 UTC. The webhook HTTP endpoint kept returning 200 OK to GitHub, so GitHub considered every delivery successful and never retried. From the outside, caretaker was silently failing.

The proximate cause was a stuck rate-limit cooldown in the deployed image (which predated PR #659's self-heal fix). The deployed image's `RateLimitCooldown._blocked_until` was set to ~58 minutes in the future, even though GitHub's bucket showed plenty of headroom. The webhook handler checks `cooldown.is_blocked()` before dispatching events; with the cooldown engaged, every webhook was acked-but-skipped (`outcome=deferred_cooldown`).

The PR #659 fix was on `main` but had not been deployed to the cluster (last `deploy-mcp.yml` success was 04-29 00:59 UTC, ~36 hours before the fix landed). Triggering the redeploy (which builds from main and includes the fix) cleared the stuck cooldown by virtue of starting the process fresh.

## Why this took so long to diagnose

Three independent observability failures stacked on top of each other:

### 1. The metric `caretaker_rate_limit_cooldown_seconds` is a snapshot, not a live counter

`_publish_rate_limit_metrics()` writes `seconds_remaining` to a Prometheus gauge **only when the cooldown is set or extended** — never on a timer, never on `is_blocked()` checks. Once the cooldown was engaged with value 456s, the gauge stayed at 456 indefinitely. We sampled it 9 seconds apart and got the same value, which initially looked like the cooldown was "stuck" — when in fact, the underlying `_blocked_until` timestamp was a real datetime that may already have elapsed.

To know whether the cooldown is *currently* engaged, the only reliable signal is whether the `caretaker_webhook_events_total{outcome="deferred_cooldown"}` counter is incrementing. We had to send a deliberate webhook and re-sample the counter to confirm the cooldown was still actively blocking — not just stale on the gauge.

### 2. Python stdout was fully buffered (no `PYTHONUNBUFFERED=1`)

`uvicorn caretaker.mcp_backend.main:app` runs as PID 1 with stdout going through a pipe to kubelet, which means Python applies block buffering by default. Caretaker's lifespan startup, dispatcher, and consumers all use `logger.info` for breadcrumbs — none of which reached `kubectl logs` until the buffer filled or was flushed. The pods showed only the two pre-app uvicorn lines (`Started server process`, `Waiting for application startup`), with no app-level output for 12+ minutes.

This made the lifespan-hang invisible. Without log output, we couldn't see which `await` in the lifespan was blocking, what state the eventbus consumer was in, what Redis Streams group was being joined, etc.

### 3. The deployment has liveness/readiness probes but no startupProbe

`infra/k8s/caretaker-mcp-deployment.yaml` defines liveness (`initialDelaySeconds=10, periodSeconds=15`) and readiness (`initialDelaySeconds=5, periodSeconds=10`) probes pointed at `/health` — both verified live on the deployed pod. What's *missing* is a `startupProbe`, which is what would handle long initial startup gracefully:

- The liveness probe's `failureThreshold=3 × periodSeconds=15 = 45s` window is far shorter than today's 12-minute lifespan hang. If uvicorn weren't already binding port 8000 during the lifespan (which it apparently is, otherwise the pod would have been killed and restarted in a death loop), this combination would have crashlooped the pod.
- Without a `startupProbe`, the liveness window starts immediately at `initialDelaySeconds=10s`, leaving zero margin for any genuine slow startup. A `startupProbe` with `failureThreshold=30 × periodSeconds=10 = 5 min` graduates to liveness only after the app is healthy for 5 minutes, which fits today's slow startup cleanly.
- The bigger silent-failure mode is logs: even with probes, the 12-minute hang produced zero stderr/stdout because Python was fully buffered (see #2 above). So while K8s arguably had the right signal *internally*, the operator had no way to see what the pod was doing during startup.

## Contributing factors

- **The self-heal logic in PR #659 has a structural gap.** `RateLimitCooldown.maybe_clear_if_healthy()` is only called from `record_response_headers()` — i.e., after a successful response from GitHub. But while the cooldown is engaged, the webhook handler skips dispatch entirely; no GitHub API calls happen; no responses come back; the self-heal never runs. The fix is one-sided: it prevents fresh stuck cooldowns from sticking long if GitHub responds during the window, but cannot rescue a cooldown that engages with no concurrent traffic.

- **The cron-based `maintainer.yml` workflow had been retired but its scheduled invocations continued to fail loudly** (cron-fires-on-deleted-workflow-name pattern), creating noise that initially distracted from the real issue. Until the deploy-mcp workflow run history was checked, it was tempting to attribute caretaker silence to the cron failures, when in fact those have no effect on webhook processing.

- **No admin endpoint exists to inspect or clear the cooldown without restarting the process.** Once a cooldown is engaged, an operator's only recourse is `kubectl rollout restart`, which cycles all pods, drops in-flight work, and resets unrelated state.

- **The `caretaker-mongo` DNS lookup failure inside the pod was a red herring.** During diagnosis, `socket.gethostbyname("caretaker-mongo")` failed, suggesting a Mongo connectivity problem. The actual `MONGODB_URL` value pointed at the FQDN `caretaker-mongo.mongo.cosmos.azure.com` which resolved fine. An operator unfamiliar with the URL parsing would lose 10–15 minutes here.

- **The `deploy-mcp.yml` workflow uses a placeholder image then patches.** The first applied manifest references `your-acr.azurecr.io/caretaker-mcp:v1.0.0` (literal placeholder); a second `kubectl set image` patch swaps to the real image. This produces a brief `ImagePullBackOff` window and creates two replicasets every deploy. Brittle but not the root cause.

## Detection gaps

- Caretaker has no internal "I am not processing webhooks" alert. The metric `caretaker_webhook_events_total{outcome="deferred_cooldown"}` is the canonical signal, but no alerting rule fires on a sustained nonzero rate of that outcome.
- No alert on cooldown engagement duration. A `caretaker_rate_limit_cooldown_seconds > 60` gauge fires nothing.
- No alert on consumer group lag growth in Redis Streams. The `caretaker:events` stream had `lag=40` for 30+ minutes; nothing notified.
- The bot's *absence of activity* is itself a signal we didn't catch. A simple "if no PullRequestEvent / IssueCommentEvent from the-care-taker[bot] in 30 min, page" would have detected this.

## Action items

### Must-fix before next release (operational hardening)

1. **Add a `startupProbe` to `infra/k8s/caretaker-mcp-deployment.yaml`**.
   - `httpGet: /health` on port 8000, `failureThreshold=30`, `periodSeconds=10` (= ~5 min total budget).
   - Liveness and readiness probes are already in place; the gap is that they begin immediately and have a tight 45s failure window that doesn't accommodate slow startup. A `startupProbe` graduates to liveness/readiness only after the app is genuinely up.
   - Pair with a small `initialDelaySeconds` reduction on the liveness probe once `startupProbe` covers the slow-start window.

2. **Add `PYTHONUNBUFFERED=1` to the deployment env permanently** (not as an ad-hoc operator override).

3. **Configure Python logging to stream INFO to stdout in `mcp_backend/main.py`**.
   Even with `PYTHONUNBUFFERED=1`, the app logger may not be wired to stdout depending on uvicorn's config. Verify `caretaker` logger emits INFO-level records to stdout so the lifespan breadcrumbs (`fleet bearer auth configured`, `webhook event-bus consumer started`, etc.) actually reach `kubectl logs`.

### Must-fix in code (correctness)

4. **Make the cooldown self-heal reachable from the deferred state.**
   Add a periodic background task (every 60s, say) that, when the cooldown is engaged, makes a single `GET /rate_limit` call to GitHub and feeds the response through `record_response_headers()`. This guarantees the self-heal eventually fires even if no other GitHub traffic happens. Alternative: cap the maximum cooldown wallclock to 5 minutes regardless of GitHub's `Retry-After` header — the worst case for an early-clear is one extra request that gets rate-limited again and re-extends.

5. **Add an admin endpoint `POST /api/admin/cooldown/reset`** (auth-gated). Currently the only operator hatch is restarting all pods. A surgical reset endpoint would have shortened today's incident by 2+ hours.

6. **Investigate why the cooldown was engaged in the first place.**
   The deployed image (pre-restart) had its cooldown engaged with `_blocked_until = now + ~7.5 minutes`. Either GitHub legitimately rate-limited caretaker, or something parsed a header wrong. Audit the `record_rate_limit_response` callers — particularly any background tasks (fleet heartbeat receiver? installation discovery?) that might fire many concurrent GitHub calls at startup.

### Should-fix (reliability)

7. **Add Prometheus alerting rules** for:
   - `rate(caretaker_webhook_events_total{outcome="deferred_cooldown"}[5m]) > 0` for 5+ min
   - `caretaker_rate_limit_cooldown_seconds > 600` (cooldown longer than 10 min)
   - `redis_stream_consumer_group_lag{stream="caretaker:events"} > 100` for 5+ min
   - Bot inactivity: `time() - timestamp(caretaker_webhook_events_total{outcome=~"active.*"}) > 1800`

8. **Clean up zombie consumers in the `agents` consumer group on Redis Streams.**
   The group had 16 consumers with 47-hour idle times — survivors of pods deleted long ago. Add a janitor that deletes consumers idle more than 1 hour. The reaper task already redelivers messages from idle consumers but doesn't unregister them.

9. **Stop the `maintainer.yml` cron from firing.**
   The workflow was retired but GitHub still has the registration and runs the deleted file every ~6 hours, producing failure noise. `gh workflow disable maintainer.yml` (find the workflow ID) or accept a docs PR explicitly removing the registration.

### Nice-to-have

10. Replace `deploy-mcp.yml`'s placeholder-image-then-patch flow with a single manifest that uses the correct image from the start (e.g., `envsubst` or kustomize image override).

11. Drop the `_publish_rate_limit_metrics` snapshot semantics in favor of a live gauge. Either compute `seconds_remaining` on every `/metrics` scrape, or update the gauge on every `is_blocked()` call. The current "snapshot at last set" semantics make the metric near-useless during incidents.

## Lessons

- **A 200-OK ack is not the same as "the work was done."** Caretaker's webhook handler returns `WebhookAck(status="accepted", dispatched=...)` — the `dispatched` boolean was always available but never plumbed into the response and never alerted on. GitHub considered every delivery successful while caretaker silently dropped them on the floor.

- **Production observability is binary: either you can see what's happening, or you can't.** Tonight, two of three layers (snapshot-not-counter metric, buffered stdout) plus a missing `startupProbe` made the system completely opaque during the 12-min lifespan hang. Each in isolation might have been workable; stacked, they made the system completely opaque. Treat "operator can read logs of a hung pod" as a P0 invariant, not a nice-to-have.

- **A production incident report tells you what to deploy, not just what to land on `main`.** PR #659's commit message described an "active production incident" but the deploy that would have fixed it was a separate manual step. A workflow that auto-deploys on merges to `main` for fix-tagged PRs (or at least an alert when a fix-tagged PR is merged but not deployed) would have closed today's deploy-lag gap.

- **A self-heal that requires a specific event flow is fragile.** PR #659 self-heals when GitHub responds — but the same conditions that make the self-heal necessary (cooldown engaged) also prevent GitHub responses from arriving. Self-heal logic should run on its own clock, not piggyback on the failing path.

## Resolution / current state

- Pods restarted at 23:39 UTC with `PYTHONUNBUFFERED=1`. Both pods Running, `cooldown=0`, dispatch_mode=active.
- Webhook test events confirm intake → dispatch path is working (`outcome=active`).
- One observed `outcome=deferred_cooldown` blip on a `check_suite` event on pod `tmlrq` after restart — cooldown can re-engage locally, suggesting AI #4 (self-heal trigger gap) is still relevant. Will reproduce silently if the same conditions recur.
- No formal probe / alerting / admin-reset changes deployed yet. **Recurrence risk: medium-high** until AIs #1, #4, #7 land.
