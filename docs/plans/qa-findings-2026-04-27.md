# QA Run Findings — 2026-04-27 (caretaker-qa testbed)

This document captures findings from a live-fire QA cycle of caretaker
against [ianlintner/caretaker-qa](https://github.com/ianlintner/caretaker-qa)
following the merge of PR [#621](https://github.com/ianlintner/caretaker/pull/621)
(backend-routed event bus + auto-bootstrap + Mongo fleet store +
reconciliation scheduler) and PR [#620](https://github.com/ianlintner/caretaker/pull/620)
(pr-reviewer two consumer bugs).

Cycle followed [`.github/skills/caretaker-qa-cycle.md`](https://github.com/ianlintner/caretaker/blob/main/.github/skills/caretaker-qa-cycle.md).

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

The example `gh api graphql … --jq` filter for `match("<!-- caretaker:…")` in [`.github/skills/caretaker-qa-cycle.md`](https://github.com/ianlintner/caretaker/blob/main/.github/skills/caretaker-qa-cycle.md) is reliable when run against the REST `comments` endpoint but flaky against the GraphQL `pullRequest.comments.nodes[].body` field — the marker landed inside a fenced code block that contained another `<!--` literal in the issue body, and the regex needs `?:` around the trailing class to avoid an early bail. Affects only the example, not behavior.

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

### F-5. `.caretaker.yml` autonomy keys never loaded by server-side dispatch — **REGRESSION, FIXED**

Surfaced when [caretaker#622](https://github.com/ianlintner/caretaker/pull/622) sat at 100% readiness without an APPROVE or merge. The active dispatcher's `GitHubAppContextFactory._load_config` reads `.github/maintainer/config.yml` exclusively. The central caretaker repo's `pr_agent.merge_authority.mode: gate_and_merge` and `pr_agent.review.auto_approve_caretaker_prs: true` lived in `.caretaker.yml` at the repo root — a path no caretaker code path auto-discovers. Result: those keys silently fell back to defaults (`auto_approve_caretaker_prs: False`, `merge_authority.mode: advisory`, agent memory backend = SQLite) and autonomy stopped working.

Fix in [#625](https://github.com/ianlintner/caretaker/pull/625): inlined the four missing keys (`memory_store`, `mongo`, `pr_agent.merge_authority`, `pr_agent.review.auto_approve_caretaker_prs`) into `.github/maintainer/config.yml`, deleted `.caretaker.yml`, updated docstring examples and the qa-cycle skill recipe (F-2). Single source of truth. Verified post-merge by the next webhook tick honoring the autonomy keys.

### F-6. Duplicate-PR dedup picked older over newer target version — **BUG, FIXED**

Symptom: caretaker-qa#67 (target v0.25.0) was closed as "duplicate of #39 (both address pkg:caretaker)" — but #39 was a stale 2-day-old upgrade PR targeting v0.19.4. The retry caretaker-qa#70 was closed for the same reason against caretaker-qa#69 (also v0.25.0, but earlier). The dedup heuristic in `pr_agent/pr_triage.close_duplicate_fix_prs` selected the **oldest** PR by `created_at` as the survivor for **all** group keys, including `pkg:*` (upgrade) groups where the canonical "first PR opened" rationale doesn't apply — newer upgrade PRs target newer versions.

Fix in [#628](https://github.com/ianlintner/caretaker/pull/628): split the survivor selection by group key. CVE groups keep oldest-wins (canonical review history). `pkg:*` groups switch to **highest target version** (parsed from "to vX.Y.Z" in the title), with `created_at` as a tiebreak and PR number as the final deterministic break. Three new regression tests including an explicit replay of the qa#39-vs-qa#70 scenario.

### F-7. Auto-approve handler refused `is_maintainer_bot_pr` PRs — **REGRESSION (pre-existing), FIXED**

Surfaced once F-5 was fixed and [caretaker#626](https://github.com/ianlintner/caretaker/pull/626) (a `chore/releases-json-v0.25.0` PR auto-opened by the `update-releases-json` workflow) sat at 100% readiness without an APPROVE. The state machine in `pr_agent/states.py:530` emits `request_review_approve` when `pr.is_caretaker_pr OR pr.is_maintainer_bot_pr`, but `_handle_auto_approve` at `agent.py:1034` only accepted `is_caretaker_pr` — silently refusing every `chore/releases-json-vX.Y.Z` PR. Pre-F-5 the symptom was masked because nothing auto-approved at all.

Fix in [#627](https://github.com/ianlintner/caretaker/pull/627): relaxed the defensive guard to accept either flag. Both upstream gates already enforce the safety set ("CI passing + caretaker-or-bot-authored + not changes_requested + not already approved + not FIX_REQUESTED"). Live test will fire on the next `chore/releases-json-vX.Y.Z` PR (e.g. v0.26.0 release pipeline).

## Triage

| Finding | Severity | Action |
|---|---|---|
| F-1 | informational | none — expected migration shape |
| F-2 | minor docs | fixed in #625 (caretaker-qa-cycle skill monitor recipe) |
| F-3 | confirms healthy | none |
| F-4 | confirms healthy | none |
| F-5 | regression, blocking autonomy | **fixed in #625** |
| F-6 | bug, stalls upgrade chain | **fixed in #628** |
| F-7 | pre-existing gap, blocks release pipeline | **fixed in #627** |

## v0.25.0 release + fleet upgrade

Cycle ended in a clean v0.25.0 cut and full fleet upgrade. Order:

1. F-5 fix landed in [#625](https://github.com/ianlintner/caretaker/pull/625) (autonomy unblocked on the central repo).
2. `release-prepare` workflow auto-bumped pyproject, tagged `v0.25.0`, pushed the GitHub Release. `release-publish` shipped 2 wheels to PyPI. `update-releases-json` opened [#626](https://github.com/ianlintner/caretaker/pull/626) automatically.
3. F-7 fix landed in [#627](https://github.com/ianlintner/caretaker/pull/627). Backend redeployed at the F-7 sha.
4. F-6 fix landed in [#628](https://github.com/ianlintner/caretaker/pull/628).
5. Fleet upgrade rolled to all 12 known consumer repos in one batch — every repo now pinned to `v0.25.0` with the ~80-line thin streaming workflow:

| Repo | Was | Now |
|---|---|---|
| caretaker-qa | 0.24.0 | 0.25.0 |
| space-tycoon | 0.22.3 | 0.25.0 |
| rust-oauth2-server | 0.22.3 | 0.25.0 |
| AI-Pipeline | 0.19.4 | 0.25.0 |
| Example-React-AI-Chat-App | 0.19.4 | 0.25.0 |
| algo_functional | 0.19.4 | 0.25.0 |
| tail_vapor | 0.19.4 | 0.25.0 |
| portfolio | 0.19.4 | 0.25.0 |
| flashcards | 0.19.4 | 0.25.0 |
| python_dsa | 0.19.6 | 0.25.0 |
| kubernetes-apply-vscode | 0.19.4 | 0.25.0 |
| audio_engineer | 0.19.6 | 0.25.0 |

Side effect already observed: space-tycoon's failing `Caretaker Maintainer` workflow (401 Invalid token audience from the empty `CARETAKER_OIDC_AUDIENCE` env var) was unblocked by the upgrade because v0.25.0 includes [#619](https://github.com/ianlintner/caretaker/pull/619)'s default-audience fallback.

## Closures

- [caretaker-qa#64](https://github.com/ianlintner/caretaker-qa/issues/64) closed (scenario-15 PASS, `caretaker:qa-passed`).
- [caretaker-qa#65](https://github.com/ianlintner/caretaker-qa/issues/65) closed (scenario-16 PASS, `caretaker:qa-passed`).
- [caretaker-qa#66](https://github.com/ianlintner/caretaker-qa/pull/66) — left for QA repo maintainer; verification value already captured.
- [caretaker-qa#67](https://github.com/ianlintner/caretaker-qa/pull/67) — closed by F-6 dedup; superseded.
- [caretaker-qa#69](https://github.com/ianlintner/caretaker-qa/pull/69) — caretaker's own auto-bump merged (qa pin to v0.25.0).
- [caretaker-qa#70](https://github.com/ianlintner/caretaker-qa/pull/70) — closed by F-6 dedup against #69; superseded.

## Next cycle prep

- Verify the F-7 fix on the next `chore/releases-json-vX.Y.Z` PR (will fire on v0.26.0 release pipeline).
- Verify the F-6 fix on the next collision in the wild (any future dual-target upgrade PR for the same package).
- Scenario-17 (consumer-side `caretaker stream` → `/runs/{id}/trigger` → bus path) is unblocked now that the entire fleet runs the thin workflow.
