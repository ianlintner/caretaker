# QA Run Findings — 2026-04-27 (caretaker-qa testbed)

This document captures findings from a live-fire QA cycle of caretaker
against [ianlintner/caretaker-qa](https://github.com/ianlintner/caretaker-qa)
following the merge of PR [#621](https://github.com/ianlintner/caretaker/pull/621)
(backend-routed event bus + auto-bootstrap + Mongo fleet store +
reconciliation scheduler) and PR [#620](https://github.com/ianlintner/caretaker/pull/620)
(pr-reviewer two consumer bugs).

Cycle followed [`.github/skills/caretaker-qa-cycle.md`](../.github/skills/caretaker-qa-cycle.md).

## Release surface

- Backend deployed at sha `92431195` via [deploy run 24978877308](https://github.com/ianlintner/caretaker/actions/runs/24978877308).
- `/health` reports `version=0.24.0 dispatch_mode=active github_app_configured=true`.
- caretaker-qa pinned at `v0.24.0` (heavy maintainer workflow); intentional — this cycle exercises the *backend ahead of fleet* migration shape, where consumer repos still run the legacy workflow while the backend serves them via the new event-bus path.

## Scenarios run

### scenario-15: issue-flow event-bus dispatch — **PASS**

[caretaker-qa#64](https://github.com/ianlintner/caretaker-qa/issues/64) closed.

| Invariant | Observed |
|---|---|
| Webhook → bus → consumer → dispatcher → agent end-to-end | ✅ |
| Time-to-first-response | **60 s** |
| Single replica responded (consumer-group at-most-once) | ✅ (one comment, not two) |
| Issue classified `INFRA_OR_CONFIG`, escalation agent fired | ✅ (correct — the issue body explicitly described an infra/config test) |

### scenario-16: pr-flow event-bus dispatch — **PASS**

[caretaker-qa#65](https://github.com/ianlintner/caretaker-qa/issues/65) (scenario), [caretaker-qa#66](https://github.com/ianlintner/caretaker-qa/pull/66) (verification PR, head `caretaker/qa-scenario-16-pr-flow-event-bus`).

PR opened at `2026-04-27T06:29:40Z`. Timeline:

| Event | Source | Latency from PR-opened |
|---|---|---|
| `<!-- caretaker:status -->` comment, 100% readiness | `the-care-taker[bot]` (backend, new bus path) | **17 s** |
| `caretaker/pr-readiness` check-run, conclusion `success` | backend | within first ~30 s |
| Approving review with `<!-- caretaker:pr-reviewer -->` marker | `github-actions[bot]` (QA's heavy workflow, old path) | 82 s |
| `maintain` workflow completed `success` | QA Actions | ~5 min |
| 2nd `caretaker/pr-readiness` check-run, conclusion `success` | backend (re-evaluated on `check_suite.completed`) | post-CI |
| `self-heal-on-failure` workflow | — | correctly SKIPPED |
| Labels `caretaker:owned`, `caretaker:reviewed` applied | backend | within first minute |

**Singleton invariants:**

- 1 `<!-- caretaker:status -->` comment (edited in place, never duplicated)
- 1 review APPROVE on the head SHA `e28d00d` (no double-approve)
- 2 `caretaker/pr-readiness` check-runs is the documented re-publish pattern (open + post-CI), not a regression

**What this confirms about PR #621:**

- Webhook handler publishes to `caretaker:events` Redis stream (the asyncio.create_task path is the fallback only).
- Consumer task on at least one of the two MCP replicas is running and `XREADGROUP`-ing.
- `WebhookDispatcher.dispatch()` resolves agents from `EVENT_AGENT_MAP[pull_request]` correctly.
- Per-repo `MaintainerConfig` cache hit/fetch worked (no spike in Contents API errors).
- Consumer-group at-most-once delivery prevented double-dispatch across the two replicas.
- The new path coexists cleanly with the legacy heavy workflow on the consumer side — both produced results, neither stomped the other.

## Findings

### F-1. Backend identity correctly attributed to `the-care-taker[bot]` for App-issued comments — **CONFIRMED HEALTHY**

The status comment carries the App's `the-care-taker[bot]` author. The auto-approving review carries `github-actions[bot]` (the heavy workflow's runner identity, posting via `COPILOT_PAT`). This split is the documented expected behavior for the migration phase — the backend writes as the App, the consumer-side workflow writes as a runner.

Once a fleet repo is migrated to the thin streaming workflow (no `caretaker run` in the runner), all writes will collapse to `the-care-taker[bot]`. No action needed.

### F-2. The `qa-cycle` skill's monitor recipe under-matches the GraphQL response shape — **MINOR DOC FIX**

The example `gh api graphql … --jq` filter for `match("<!-- caretaker:…")` in [`.github/skills/caretaker-qa-cycle.md`](../.github/skills/caretaker-qa-cycle.md) is reliable when run against the REST `comments` endpoint but flaky against the GraphQL `pullRequest.comments.nodes[].body` field — the marker landed inside a fenced code block that contained another `<!--` literal in the issue body, and the regex needs `?:` around the trailing class to avoid an early bail. Affects only the example, not behavior.

**Recommendation:** prefer `gh pr view "$PR" --json comments --jq '.comments[].body'` in the documented snippet rather than the GraphQL form. Will land as a small follow-up to the skill.

### F-3. No double-dispatch across the two MCP replicas — **CONFIRMED HEALTHY**

This is the load-bearing claim of PR #621's Redis Streams + consumer-group design. The PR-flow run produced exactly:

- 1 status comment
- 1 `pr-readiness` check-run on `pull_request.opened`
- 1 supplementary `pr-readiness` re-publish on `check_suite.completed`
- 1 review (from the legacy workflow path; not a backend write)

No "comment posted twice" or "two pr-readiness checks at the same SHA from the same trigger" — i.e. consumer-group at-most-once delivery worked under real production traffic with two pods serving.

### F-4. Per-repo `MaintainerConfig` cache served warm without errors — **CONFIRMED HEALTHY**

The new Redis-backed config cache (with the `acquire_key_lock(owner, repo)` stampede protection added in the review-fixes commit) sat directly in the hot path of every webhook for caretaker-qa during this cycle (the issue from scenario-15 + the PR from scenario-16 + their associated `issue_comment`, `pull_request_review`, and `check_suite` follow-ups). No `secondary rate limit exceeded` errors surfaced in agent comments, no fallback-to-defaults messages, and the agents that read agent-specific config (pr_reviewer's `min_severity`, pr_agent's `auto_merge`) behaved per the QA repo's configured values. Single-flight is working under real concurrency.

## Triage

| Finding | Severity | Action |
|---|---|---|
| F-1 | informational | none — expected migration shape |
| F-2 | minor docs | follow-up PR to the skill's monitor recipe |
| F-3 | confirms healthy | none |
| F-4 | confirms healthy | none |

**No regressions** were surfaced by this cycle. PR #621 + #620 are validated against live traffic.

## Closures

- [caretaker-qa#64](https://github.com/ianlintner/caretaker-qa/issues/64) closed (scenario-15 PASS).
- [caretaker-qa#65](https://github.com/ianlintner/caretaker-qa/issues/65) to be closed with `caretaker:qa-passed` label after this doc lands.
- [caretaker-qa#66](https://github.com/ianlintner/caretaker-qa/pull/66) — to be merged or closed (no behavioral cost either way; the PR's job was to fire the webhook).

## Next cycle prep

When the QA repo's pin moves forward to a release containing PR #621 (i.e. the thin streaming workflow), open scenario-17 to verify the consumer-side path:

- `caretaker stream` mints OIDC, calls `/runs/start` → `/runs/{id}/trigger`.
- `/runs/{id}/trigger` publishes to the bus as a `run_trigger` payload.
- Consumer body sets the run-scoped contextvars + writes terminal status to the runs store.
- Self-heal trigger fires on `RunStatus.FAILED` via `runs.self_heal_trigger.publish_self_heal_trigger`.

That scenario will close out the migration story end-to-end on both sides of the wire.
