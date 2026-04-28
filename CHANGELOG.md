# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

### Added

- **Pre-dispatch comment gate for the GitHub App webhook receiver.**
  New module `caretaker.github_app.comment_gate` runs in front of
  `WebhookDispatcher.dispatch` for `issue_comment`,
  `pull_request_review`, and `pull_request_review_comment` events. Two
  jobs:

  *Self-echo loop prevention.* When a webhook delivery comes from a
  bot actor with a `<!-- caretaker:... -->` marker (caretaker's own
  output bouncing back), the gate short-circuits with
  `outcome="self_echo"`. No installation token is minted, no agent
  runs. Defence in depth on top of the per-agent marker checks that
  already exist.

  *Explicit human-intent recognition.* When a non-bot actor invokes
  one of the existing trigger tokens — `@caretaker`, `@the-care-taker`,
  `/caretaker`, `/maintain` — the gate surfaces a structured log line
  (`webhook gate outcome=human_intent ...`) and an extra
  `caretaker_webhook_events_total{outcome="human_intent"}` sample.
  Operators can grep this to confirm a `@caretaker` mention reached
  the backend, separate from the generic `outcome="active"` /
  `"shadow"` rates. Underlying agent dispatch is unchanged.

  Mode is selected by `CARETAKER_WEBHOOK_COMMENT_GATING`:
  `off` (legacy / instant rollback), `advise` (default — self-echo
  skip + human-intent observability), `enforce` (also drop comment
  events with no explicit trigger). `advise` is the safe default
  because it only changes behaviour for self-echo events, which were
  already expected to no-op.

  Surfaced after a `@caretaker take this over` comment on PR #630
  produced no observable backend signal — the gate now makes such
  triggers loud in the metric stream and lays the groundwork for a
  future "spawn dedicated takeover worker" path.

### Fixed

- **`caretaker-mcp` Prometheus HTTP middleware no longer silently fails
  to register at startup.** `init_metrics(app, ...)` was being called
  from inside the FastAPI lifespan handler, which Starlette rejects with
  `RuntimeError: Cannot add middleware after an application has started`.
  The error was swallowed by a `try/except` that demoted it to a warning,
  so pods stayed up — but `caretaker_http_server_requests_total` and
  `caretaker_http_server_request_duration_seconds` never incremented in
  production, leaving the RED-floor dashboards empty. The middleware now
  registers at module import time (right after `app = FastAPI(...)`),
  matching the layout that `tests/test_metrics.py` has had all along; the
  `/metrics` ASGI sidecar — which legitimately needs an event loop —
  stays in lifespan. Regression test in
  `tests/test_mcp_backend_metrics_wired.py` asserts the counter
  increments after a `TestClient` hits `/health`. Surfaced while
  debugging an unrelated `@caretaker` PR-takeover question (PR #630)
  where the empty counters made it impossible to tell whether the
  webhook dispatcher had run.

- **`pr_reviewer.handoff_review_consumer` two-bug fix surfaced by the
  v0.24.0 live QA cycle on caretaker-qa#63.**

  *Bug 1 — caretaker harvested its own invitation.* The hand-off
  invitation comment embeds a worked example of the `caretaker-review`
  payload (so the agent knows what shape to emit), which means the
  invitation contains the same `<!-- caretaker:review-result -->`
  marker the response uses. The previous `_is_caretaker_authored`
  predicate (`has_caretaker_marker AND NOT has_response_marker`)
  misclassified the invitation as an agent reply. Production was
  saved only because the example JSON intentionally contains `//`
  comments (invalid JSON → `parse_review_payload` returned `None`);
  a future copy edit producing strictly-valid example JSON would
  have posted a fake formal review with placeholder content.
  Detection now uses the per-backend invitation markers
  (`CLAUDE_CODE_REVIEW_MARKER` / `OPENCODE_REVIEW_MARKER`) directly.

  *Bug 2 — Claude Code's actual replies were never harvested.* In
  practice, Claude Code's output formatter drops the literal
  `<!-- caretaker:review-result -->` HTML comment while keeping the
  `caretaker-review` JSON fence intact. The previous consumer
  required the HTML marker as a precondition, so every real-world
  hand-off review was silently skipped. The fence tag is unique
  enough to be the primary signal; the HTML marker stays documented
  as optional belt-and-suspenders.

  Two new regression tests pin both fixes — `_is_caretaker_authored`
  against the verbatim invitation body from caretaker-qa#61, and the
  fence-without-marker path against a faithful reproduction of
  Claude Code's actual reply on caretaker-qa#63.

- **`caretaker/pr-readiness` no longer dangles in `in_progress` after
  caretaker escalates a PR.** The check now transitions to a terminal
  conclusion as soon as the PR enters the escalated state — `neutral`
  in advisory mode (the default; informational only) or `failure` in
  gate modes (still blocks branch protection). `conclusion == "success"`
  still wins so a human approval after escalation correctly flips the
  check back to success rather than locking it at terminal-neutral.
  Motivating incident: PR #613, where caretaker scored the PR at 30%
  immediately after open (before CI had started), escalated within
  45s, and the check stayed `in_progress` indefinitely even after CI
  passed and a Claude review posted.
### Added

- **BYOCA hand-off reviews now appear in the Reviews tab.** When a
  hand-off agent (Claude Code, opencode, …) replies to caretaker's
  invitation comment, the reply can include a
  `<!-- caretaker:review-result -->` marker followed by a fenced
  `caretaker-review` JSON block. Caretaker's PR reviewer harvests the
  payload on its next cycle and re-posts it as a *formal* GitHub PR
  review via the Reviews API — inline comments anchored to lines,
  verdict (`APPROVE` / `COMMENT` / `REQUEST_CHANGES`), summary,
  attribution to the originating agent. The review is authored by
  `the-care-taker[bot]`, so it counts toward branch-protection rules
  that allow bot reviewers and shows up alongside human reviews under
  the **Reviews** tab. Agents that don't include the marker still
  post a regular issue comment, just outside the Reviews tab.

  Implemented via:
    - new `caretaker.pr_reviewer.handoff_review_consumer` module with
      `parse_review_payload` (tolerant of malformed input — bad
      payloads are recorded once and skipped permanently) and
      `consume_handoff_reviews`.
    - new `TrackedPR.consumed_handoff_review_comment_ids` field for
      idempotency across cycles / webhook re-deliveries.
    - new `harvested` field on the `pr_reviewer` agent's run report
      so operators can distinguish caretaker's own inline reviews
      from harvested-from-agent reviews.
    - hand-off invitation now documents the `caretaker-review` schema
      so the agent knows how to opt in.

BYOCA — Bring Your Own Coding Agent. Generalises the existing Claude Code
hand-off path into a pluggable registry so opencode (and future agents
like codex, gemini, hermes) can coexist with Claude Code as first-class
options.

### Added

- **`caretaker.coding_agents` package** — new module hosting the
  `CodingAgent` protocol, `HandoffAgent` base class, concrete
  `ClaudeCodeAgent` / `OpenCodeAgent` subclasses, and
  `CodingAgentRegistry`. Each hand-off agent owns a unique HTML-comment
  marker so per-PR attempt counts don't cross-contaminate.
- **`OpenCodeExecutorConfig`** in `caretaker.config` plus an open-ended
  `executor.agents: dict[str, HandoffAgentConfig]` map for additional
  agents declared per repo. `executor.provider` is now an open string
  (validated at startup against the registry) instead of a closed enum.
- **`pr_reviewer.complex_reviewer` config field** — selects which
  hand-off agent the PR reviewer uses for complex PRs (`claude_code`,
  `opencode`, …). Default keeps existing behaviour.
- **`pr_reviewer.opencode_label` / `opencode_mention` config fields**
  paired with the new `OPENCODE_REVIEW_MARKER` so opencode review
  hand-offs don't collide with Claude review hand-offs.
- **Generic `agent:<name>` PR labels** — caretaker now resolves
  `agent:opencode`, `agent:codex`, etc. against the registry instead of
  only honouring the legacy `agent:custom` alias. `agent:custom` still
  works.
- **`RouteOutcome.CUSTOM_AGENT`** + `RouteResult.agent_name` — new
  generic outcome for any registered hand-off agent. The legacy
  `RouteOutcome.CLAUDE_CODE` value is preserved for one release as an
  alias when ``agent_name == "claude_code"``.
- **`opencode.yml` / `opencode-review.yml` workflow templates** in
  `setup-templates/templates/workflows/`. The maintainer agent's sync
  issue lists them in a new "Optional templates" section so consumer
  repos opt in only when they enable the matching feature.
- **`doctor` row** validating `executor.provider` and
  `pr_reviewer.complex_reviewer` resolve to known agents.

### Changed

- **`ExecutorDispatcher` constructor** now takes a `registry:
  CodingAgentRegistry` argument. The legacy `claude_code_executor=`
  parameter is kept as a deprecated shim that wraps the executor in a
  one-entry registry.
- **`pr_reviewer.routing.RoutingDecision`** gains a `backend: str` field
  carrying the chosen hand-off agent name when `use_inline=False`.
- **`pr_reviewer.claude_code_reviewer.dispatch`** is a thin shim that
  pins the new `handoff_reviewer.dispatch` to `backend="claude_code"`.

### Deprecated

- `caretaker.claude_code_executor.ClaudeCodeExecutor` — alias for
  `caretaker.coding_agents.ClaudeCodeAgent`. Will be removed in the
  release after next.
- `RouteOutcome.CLAUDE_CODE` — alias for `RouteOutcome.CUSTOM_AGENT`
  with `agent_name == "claude_code"`. Switch consumers to read
  `agent_name` then drop the alias.

## [2026-W18] — 2026-04-27

- enable Anthropic prompt caching + emit cache-hit metrics (#479)
- surface 403 scope gaps as a single per-run issue (#480)
- orchestrator exit, self-heal cap, state comment dedupe (#481)
- release v0.13.0 (#482)
- caretaker doctor preflight (#483)
- consolidate bot-login detection behind caretaker.identity (#484)
- @shadow_decision decorator + AgenticConfig flags + admin endpoint (#485)
- CI failure triage migration to structured_complete + shadow (#486)
- webhook self-echo detector migration + shadow (#487)
- issue triage + dup detection migration (#488)
- readiness gate migration to structured_complete + shadow (#489)
- cascade redirection/close decisions migration + shadow (#490)
- crystallizer category reuses FailureTriage classifier + shadow (#491)
- review-comment classification migration + shadow (#492)
- executor routing migration + shadow (#493)
- stuck-PR detection migration + shadow (#494)
- release v0.14.0 (#495)
- union :Skill + :GlobalSkill in prompt-relevant retrieval (#496)
- :FleetAlert evaluator + admin endpoint (#497)
- grouped-dependabot bisector (#498)
- cross-run retriever + readiness prompt injection (#499)
- release v0.15.0 (#500)
- flip TriageAgent live — remove dry_run override (#501)
- unblock pre-orchestrator bootstrap + caretaker doctor --bootstrap-check (#502)
- Braintrust nightly harness + per-site scorers + enforce gate (#503)
- deterministic-first fix ladder + memory embedding backfill (#504)
- sanitize_input + filter_output + checkpoint_and_rollback (#505)
- caretaker_touched/merged/operator_intervened telemetry + admin endpoint (#506)
- release v0.16.0 (#507)
- switch caretaker self-run to Azure AI Foundry / LiteLLM (#513)
- infer LLM env requirement from model prefix, not provider name (#514)
- per-shadow-site model overrides for A/B model comparison (#516)
- QA scenario 11 — Azure AI Foundry prompt-cache validation (#517)
- add --llm-probe online endpoint preflight (#518)
- pass GitHub App credentials to caretaker process for self-mint tokens (#519)
- three QA gaps — qa-scenario suppression, empty PR body close, Copilot action_required escalation guard (#520)
- ship top-3 QA findings to reduce stuck PRs/issues (#521)
- QA follow-ups — issues #522–#525 (security probe auto-detect, releases.json automation, init-workflow CLI, regression tests) (#533)
- add v0.19.0 to releases.json (#534)
- make caretaker:reviewed label creation idempotent in update-releases-json (#539)
- ensure caretaker:reviewed label exists before gh pr create (#540)
- treat rate-limit 403s as WARN instead of FAIL (#542)
- fleet-wide upgrade defaults — auto_approve + auto_ready_drafts (v0.19.1) (#543)
- add v0.19.1 to releases.json (#544)
- fleet lag regression harness — fleet.yml, weekly cron, caretaker fleet lag CLI (#545)
- add v0.19.2 to releases.json (#546)
- codify manual PR cleanup into a gated shepherd mode (#547)
- bump litellm from 1.83.0 to 1.83.7 in the uv group across 1 directory (#548)
- security: bump litellm minimum to 1.83.7 (SSTI fix) (#550)
- Phase 2 webhook dispatcher (shadow-first) (#551)
- validate ShepherdConfig numeric bounds (#552)
- wire active-mode dispatcher plumbing (#553)
- wire concrete AgentContextFactory and AgentRunner for active dispatch (#554)
- rollout runbook + dispatch mode in /health (#555)
- flip webhook dispatcher to active mode (#556)
- repair Graph2DView.tsx build break (#557)
- CHANGELOG double-newline and draft promoter Checks API gating (#560)
- self_heal classifier reads pre-cleanup log + aiohttp security bump (#561)
- security: upgrade python-dotenv 1.0.1 → 1.2.2 (CVE-2025-14974) (#566)
- security: pin python-dotenv ≥ 1.2.2 to fix CVE-2025-14974 symlink-following file overwrite (#575)
- close superseded upgrade issues on new release (#578)
- auto-approve/close/escalate caretaker PRs based on review verdict (#579)
- soft-fail on transient-only errors even when no work landed (#581)
- head-SHA idempotency for review approvals + 0.19.3 (#582)
- add v0.19.3 to releases.json (#583)
- QA test plan for orchestration / PR-lifecycle state machine (#584)
- add caretaker-repo-settings skill and plan (#586)
- proactive app_id ownership check for check_run updates (#588)
- recognise maintainer-bot PRs as first-class auto-merge family (v0.19.5) (#590)
- v0.19.6 — registry persistence, admin alerts/detail, dead routers wired, CLI fleet status (#591)
- add v0.19.6 to releases.json (#592)
- harden caretaker-mcp against OOM under webhook bursts + GitHub cooldown (#596)
- drop legacy CARETAKER_FLEET_SECRET HMAC check (#597)
- wire hierarchical edges so the knowledge graph actually links (#598)
- add v0.20.1 to releases.json (#599)
- add v0.21.0 to releases.json (#600)
- persistent CausalEventStore + parent threading across agents (#601)
- wire MergeAuthorityConfig + neutral conclusion in advisory mode (#604)
- add caretaker-qa-cycle skill + backfill releases.json v0.22.0–v0.22.2 (#605)
- add v0.22.3 to releases.json (#606)
- auto-close resolved issues when tracker is empty (#607)
- auto-close stale issues when underlying condition resolves (#608)
- sortable grids + repo/version/severity/kind filters across dashboards (#609)
- unblock readiness gate — bot approvals, terminal state, polling fallback (#610)

## [0.19.6] - 2026-04-25

Wires the **fleet registry** into the admin dashboard end-to-end so that
production deployments at `https://caretaker.cat-herding.net` light up
with real heartbeat, alert, webhook, and doctor data. Closes the gap
where every child repo in `docs/fleet.yml` was effectively invisible to
the admin UI even when it ran caretaker on schedule.

### Added

- **Persistent `SQLiteFleetRegistryStore`** (`src/caretaker/fleet/sqlite_store.py`)
  — SQLite-backed implementation of the registry with the same async API
  as the in-memory store. Keeps the most recent 32 heartbeats per repo
  in a `fleet_heartbeats` ring buffer plus the latest snapshot in
  `fleet_clients`. Activated by setting `CARETAKER_FLEET_DB_PATH`
  (default location `~/.local/state/caretaker/fleet-registry.db`); the
  legacy in-memory store remains the default to avoid surprising tests.
  Survives backend restarts so dashboard data no longer evaporates on
  every pod recycle.
- **`fleet_registry` block in `setup-templates/templates/config-default.yml`**
  — opt-in template (`enabled: false`) that points at the production
  endpoint with HMAC and timeout pre-wired. Includes a 22-line comment
  block explaining the contract.
- **`CARETAKER_FLEET_SECRET` env wiring in `setup-templates/templates/workflows/maintainer.yml`**
  — exported in all three `env:` blocks (bootstrap-check, run, and
  self-heal-on-failure). Indentation across the workflow normalised to
  2-space nesting at the same time.
- **Step 2.8 in `setup-templates/SETUP_AGENT.md`** — guides Copilot
  through enabling the fleet registry on each child repo (config edit
  + secret + verification command).
- **`docs/fleet-opt-in-runbook.md`** — operator-facing runbook
  enumerating the 12 child repos in `docs/fleet.yml` with per-repo
  caveats and a four-step opt-in checklist.
- **`caretaker fleet status` CLI command** — local config inspector;
  prints the resolved `fleet_registry` block and notes whether the
  signing secret is populated.
- **`caretaker fleet register-self` CLI command** — sends one
  signed heartbeat to verify a repo's registration round-trip;
  echoes the dashboard's HTTP response for fast diagnosis.
- **Frontend `Alerts` page (`/alerts`)** — consumes
  `/api/admin/fleet/alerts` with an "open only" toggle, severity
  badges, and links back to per-repo detail. Closes the dead link
  from `AlertsBanner.tsx`.
- **Frontend `FleetDetail` page (`/fleet/:owner/:repo`)** — per-repo
  drill-down with the last 32 heartbeats, enabled-agent pills, and
  goal-health/error trends. Reachable from any row of the Fleet page.
- **`FleetClient`, `FleetClientDetail`, `FleetAlert`,
  `FleetAlertList` types** — added to `frontend/src/lib/types.ts` so
  TypeScript builds no longer rely on an implicit `FleetAlert` import.

### Changed

- **`GET /api/admin/fleet/{owner}/{repo}`** — now accepts
  `?include_history=true&history_limit=N` (max 32) and returns the
  per-repo heartbeat ring buffer alongside the client snapshot. Used
  by the new Fleet detail page.
- **`fleet/store.py` `get_store()`** — picks SQLite backing when
  `CARETAKER_FLEET_DB_PATH` is set; otherwise keeps the in-memory
  default. `reset_store_for_tests(store=None)` accepts an explicit
  store override for SQLite test fixtures.
- **`fleet/__init__.py`** — re-exports `SQLiteFleetRegistryStore`,
  `resolve_db_path`, and `DEFAULT_DB_PATH_ENV`.
- **`docs/fleet-registry.md`** — example endpoint replaced with the
  production URL `https://caretaker.cat-herding.net/api/fleet/heartbeat`;
  persistence section rewritten to describe the new SQLite seam.

### Fixed

- **Dead admin routers wired into the FastAPI app**
  (`src/caretaker/mcp_backend/main.py`):
  - `caretaker.admin.health_api` — `GET /health/doctor` now reachable;
    configured with the live `admin_data`, optional graph store, and
    fleet store on lifespan.
  - `caretaker.admin.webhooks_api` — `GET /api/admin/webhooks/deliveries`
    now reachable, and the `github_webhook` handler calls
    `register_delivery()` after each ack so the dashboard can show
    delivery history.
- **Frontend `/alerts` 404** — `AlertsBanner.tsx` linked to a route
  that did not exist; the new `Alerts` page closes that gap.
- **Workflow YAML indentation** — `setup-templates/templates/workflows/maintainer.yml`
  had inconsistent 7/9-space indentation on three `- name:` step
  headers and their `env:` maps. Normalised to 2-space nesting; YAML
  now parses cleanly without GitHub Actions' lenient mode.

## [0.19.5] — 2026-04-25

Teaches caretaker to recognise and manage **maintainer-bot PRs** — the `chore/releases-json-*` and `github-actions`-authored `chore/` PRs that the `update-releases-json.yml` workflow creates after each release. Previously these fell through to the `await_review` (human-PR) branch and were never merged automatically; now they receive full first-class treatment alongside caretaker and Copilot PRs.

### Added

- **`PullRequest.is_maintainer_bot_pr` property** — returns `True` for PRs whose `head_ref` starts with `chore/releases-json-` **or** whose author is `github-actions[bot]`/`github-actions` with a `chore/` prefix. Used as the canonical identity check throughout the merge pipeline.
- **`AutoMergeConfig.maintainer_bot_prs: bool = True`** — new flag controlling whether maintainer-bot PRs are allowed to auto-merge. Defaults to `True` because these PRs are generated deterministically by the release workflow with zero human-editable content.
- **`OwnershipAutoClaimConfig.maintainer_bot_prs: bool = True`** — parallel flag so the ownership-claim logic also picks up maintainer-bot PRs automatically.

### Changed

- **`pr_agent.states._auto_merge_allows`** — recognises `is_maintainer_bot_pr`; returns `config.auto_merge.maintainer_bot_prs` (short-circuits before the human-PR fallback).
- **`pr_agent.states.evaluate_pr` auto-approve path** — `(pr.is_caretaker_pr or pr.is_maintainer_bot_pr)` guard replaces the `is_caretaker_pr`-only check, so maintainer-bot PRs reach `request_review_approve` when CI is green.
- **`pr_agent.merge.evaluate_merge`** — handles `is_maintainer_bot_pr` before the human-PR block; appends `'Auto-merge disabled for maintainer-bot PRs'` blocker when the flag is `False`.
- **`pr_agent.ownership.should_auto_claim`** — recognises `is_maintainer_bot_pr`; returns `config.auto_claim.maintainer_bot_prs` analogously to the caretaker-PR path.

## [0.19.4] — 2026-04-25

Hotfix for `caretaker/pr-readiness` check_run app_id mismatch (issue #585) and GitHub Actions workflow permission gap.

### Fixed

- **`pr_agent._publish_readiness_check` proactive app_id ownership check** — GitHub returns 403 "Invalid app_id" when a check run update is attempted by a different GitHub App than the one that created it. Previously caretaker only caught the 403 after it arrived; now it compares `existing_check.app_id` against `self._app_id` *before* calling `update_check_run`. When the IDs differ, it creates a new check run instead (same fallback as before, but without the failed round-trip). Identity is propagated from `MaintainerConfig.github_app.app_id` through `PRAgentAdapter` → `PRAgent.__init__(app_id=...)`. If either app_id is unknown, the old best-effort behaviour is retained with a 403-fallback as a secondary safety net.
- **`CheckRun.app_id` field added** — the `app.id` field from the GitHub API response is now extracted in `get_check_runs` and stored on the `CheckRun` model, enabling the ownership comparison above.

### Changed

- **GitHub Actions workflow permissions** — `can_approve_pull_request_reviews` enabled on `ianlintner/caretaker` via `PUT /repos/.../actions/permissions/workflow`. This unblocks the `update-releases-json.yml` workflow from auto-creating PRs after a release tag is pushed.

## [0.19.3] — 2026-04-25

Hotfix release covering the duplicate-review and orchestrator-soft-fail symptoms surfaced on `main` after PR #581 merged.

### Fixed

- **`pr_agent._handle_review_approve` head-SHA idempotency** — the auto-approve path was racy across concurrent webhook + scheduled runs. When the state machine emitted `request_review_approve` more than once for the same head SHA, each run submitted a fresh `APPROVE` review, producing the "multiple reviews per caretaker PR" symptom in the field. `TrackedPR.last_approved_sha` now records the head SHA of the most recent successful auto-approval; re-entry on the same SHA short-circuits to `MERGE_READY` without calling `create_review`. A new commit advances `pr.head_sha` and re-arms the gate naturally. Defensive `is_caretaker_pr` guard added — refuses to approve PRs whose `head_ref` doesn't start with `claude/` or `caretaker/` even if upstream routing misfires.
- **`pr_agent._handle_review_close` reason newlines** — `reason` is sourced from `assess_review_verdict`'s summary slice and may carry embedded newlines from the underlying review body. The closing comment's `> {reason}` markdown blockquote rendered as fragmented sibling blocks instead of one logical quote. Reason now collapses to single-line whitespace before formatting.
- **Orchestrator transient-only soft-fail (already on main via #581)** — this release ships the fix to deployments still on 0.19.2. Rate-limit-only failures with `transient=1, non_transient=0, work_landed=False` no longer mark the workflow as failed.

### Changed

- **`ReviewConfig.auto_approve_caretaker_prs` default flipped to `False`** (was `True`). Per the PR #579 review, auto-approve rolls out per-repo behind an explicit opt-in until the head-SHA idempotency gate has soaked in production.
- **`ReviewConfig.close_on_infeasible_review` default flipped to `False`** (was `True`). The substring matcher in `assess_review_verdict` has high false-positive risk on noun phrases like "this duplicate field"; staged rollout per-repo until the heuristic is hardened or replaced by a structured decision channel.

### Notes

- Existing repos that explicitly set `review.auto_approve_caretaker_prs: true` or `review.close_on_infeasible_review: true` in their `caretaker.yaml` are unaffected — only repos relying on the old shipped defaults will see the behaviour change.
- The duplicate Copilot security PRs for the python-dotenv CVE (#563, #567, #569, #571, #573, #575, #577 — all already-superseded by merged #566) are emitted by the GitHub Copilot security flow upstream of caretaker, not by `dependency_agent`. Out of scope for this hotfix; they can be closed manually.

## [0.16.0] — 2026-04-22

Completes **Wave A** of the 2026-Q2 R&D plan (`/tmp/caretaker-rd/00-rd-master-plan.md`). Six PRs land foundation work identified by the R&D review — fleet-data unblockers, deterministic-first escalation, standing eval harness, guardrails hardening, and attribution telemetry. All new behaviour is opt-in; defaults keep runtime behaviour byte-identical to 0.15.0.

### Added

- **Deterministic-first fix ladder** (#504) — new `caretaker.self_heal_agent.fix_ladder` implements the Factory.ai / BitsAI-Fix / KubeIntellect pattern: classify the failure signature, run a bounded sandbox of candidate shell commands (ruff format, ruff check --fix, mypy install-types, pip-compile upgrade, pytest --lf), and only escalate to the LLM when every rung failed. Escalation prompt now carries the `error_sig`, the rungs that ran, and the top-5 past `:Incident` hits from `MemoryRetriever` — turning `:Incident` nodes into a retrievable corpus instead of a write-only log. `caretaker memory backfill-embeddings` populates `summary_embedding` on existing `:Incident` + `:AgentCoreMemory` nodes so the retriever has something to work with on day one. Default off (`self_heal.fix_ladder.enabled: false`).
- **Braintrust nightly eval harness + enforce gate** (#503) — new `caretaker.eval` package wires the shadow-decision store into Braintrust as paired (legacy, candidate) experiments with per-site scorers (exact-match for classification sites, llm-as-judge for generation) and a disagreement-rate scorer. `caretaker eval run --since 24h` drives the nightly workflow (`.github/workflows/nightly-eval.yml`); `.github/workflows/enforce-gate.yml` blocks PRs that flip any `agentic.<site>.mode` from `shadow` to `enforce` unless the site's 7-day rolling agreement rate clears `agentic.<site>.enforce_gate.min_agreement_rate` (defaults to 0.95). Gate fails closed on missing data.
- **Guardrails consolidation** (#505) — new `caretaker.guardrails` package lands `sanitize_input`, `filter_output`, and `checkpoint_and_rollback`. Seven external-input boundaries (issue bodies, PR review comments, webhook payloads, LLM outputs about to hit GitHub, etc.) are wired to sanitize before use; outbound GitHub writes pass through `filter_output` to strip ANSI sequences, zero-width characters, deceptive Markdown links (rewritten to explicit `visible -> target` form), caretaker-marker echoes, and sigil echoes. The `maybe_rollback` hook in `pr_agent.merge` undoes merge/label/comment side effects when a post-write invariant fails. Metrics: `caretaker_guardrail_{sanitize,filter_blocked,rollback_fired}_total`.
- **Attribution telemetry** (#506) — `TrackedPR` and `TrackedIssue` grew `caretaker_touched` / `caretaker_merged` / `caretaker_closed` / `operator_intervened` boolean fields; new `caretaker.state.intervention_detector` reconciles them against GitHub event history. Admin endpoint `GET /api/admin/attribution/summary` exposes per-repo counters so the next fleet audit can answer "did caretaker do the work vs the operator." One-shot `caretaker backfill-attribution --since 30d` reconciles existing state store rows.
- **`audio_engineer` bootstrap unblock** (#502) — root cause of the 8/8-failing `audio_engineer` workflow wave was an invalid `workflows: write` entry in `maintainer.yml` (not a valid GITHUB_TOKEN scope). Fixed the workflow + added `caretaker doctor --bootstrap-check`: an offline preflight (no GitHub, no network) that parses config.yml, reads the pinned version file, and checks env vars for every enabled agent. Consumer workflows should wire this as the first step before the full doctor call.
- **`TriageAgent` live** (#501) — removed the blanket `dry_run` override; triage now follows the orchestrator-level dry-run flag like every other agent. First site in the fleet to run live at the orchestrator level.

### Notes

- CI: `nightly-eval.yml` only has data once `@shadow_decision` sites accumulate disagreements — expect empty reports for the first few nights on quiet repos. The enforce gate skips cleanly (no flips = no gate) on PRs that don't touch `agentic.*.mode`.
- The R&D plan's Wave B (enforce-flip on Example-React, Aider-style architect/editor split, Neo4j vector index upgrade, cascade router) and Wave C (architectural refactor) are deferred to follow-ups.

## [0.15.0] — 2026-04-22

Completes Phase 3 of the 2026-Q2 agentic migration plan (`docs/plans/`). Four PRs turn caretaker's write-only memory and fleet graphs into read-and-decide surfaces, plus a first-cut Dependabot group bisector.

### Added

- **Cross-run memory retrieval** (#499) — new `caretaker.memory.retriever.MemoryRetriever` pulls the top-k most-similar past `AgentCoreMemory` snapshots (cosine when embeddings are stored, Jaccard fallback otherwise), budget-capped at ≤500 tokens. Readiness (T-A1) is the canary consumer; retrieval gated behind `memory_store.retrieval_enabled: false` + `agentic.readiness.mode ∈ {shadow, enforce}`, so existing installs are byte-identical until operators opt in. `AgentCoreMemory` graph node grew `summary`, `outcome`, `pr_number`, `issue_number`, and `summary_embedding` fields; `publish_with_embedding` computes the embedding at write time when a provider is wired. `Embedder` protocol + `LiteLLMEmbedder` stub included.
- **Skill promotion round-trip** (#496) — `InsightStore.get_relevant` now returns the union of local `:Skill` hits and fleet-promoted `:GlobalSkill` hits, deduped on signature. Hits carry a `scope: Literal["local", "global"]` field; the Foundry prompt renderer prefixes global hits with `[fleet]`. `fleet.include_global_in_prompts: true` by default — the point of this release is to close the loop. Bridges Neo4j-backed `list_global_skill_rows` through `GraphBackedGlobalSkillReader`.
- **`:FleetAlert` evaluator + admin endpoint** (#497) — `caretaker.fleet.alerts` emits `FleetAlert` nodes on four signals: `goal_health_regression` (N consecutive heartbeats below threshold), `error_spike` (≥ multiplier × prior-mean errors), `ghosted` (no heartbeat for N days), and `scope_gap` (piggyback on the #480 tracker). `FleetRegistryStore` grew a bounded per-repo heartbeat ring buffer (cap 32) so the pure evaluator has history to reason over. New `GET /api/admin/fleet/alerts?open=true` admin endpoint; resolution flow populates `resolved_at` and drops closed alerts from the `open=true` view. Off by default (`fleet.alerts.enabled: false`).
- **Grouped-dependabot bisector** (#498) — new `caretaker.dependency_agent.bisector`. Part 1 ships today: body parser for dependabot's grouped PR format (multi-package / multi-directory), classic-bisect logic with a pluggable `CIProbe` protocol, a budget-capped `bisect_grouped_dependabot_pr`, `synthesize_merge_plan`, and a `DependencyAgent` hook that fires on `caretaker:owned` + `MERGEABLE UNSTABLE` grouped PRs. Default off (`dependency_agent.bisector.enabled: false`). The CI-driven probe driver (create branch → apply subset → push → wait on CI) is deferred; the protocol wire-up is already in place for a follow-up.

## [0.14.0] — 2026-04-22

Completes Phase 2 of the 2026-Q2 agentic migration plan (`docs/plans/`). Twelve PRs landed. Every Phase 2 migration ships behind a per-domain `agentic.<name>.mode` flag that defaults to `off`, so this release is opt-in by design — no runtime behaviour changes until operators flip flags.

### Added

- **`caretaker doctor` preflight subcommand** (#483) — fails loudly on missing secrets or GitHub token scope gaps before any agent boots. Wired into `.github/workflows/maintainer.yml` as a gating `doctor` job ahead of `maintain`; the self-heal path no longer fires when `doctor` itself fails, preventing config gaps from cascading into self-heal issue storms.
- **`caretaker.identity` module** (#484) — consolidated `is_automated(login)` + `classify_identity(login, llm=...)` with a memoised 24h LLM fallback. Five ad-hoc bot-login checks migrated; the YAML dispatch-guard regex stays as a cheap prefilter, slated for A2's LLM path.
- **`@shadow_decision(name)` infrastructure** (#485) — per-site `off | shadow | enforce` decorator emits `:ShadowDecision` graph nodes on disagreement, plus `GET /api/admin/shadow/decisions` for the admin UI. `AgenticConfig` on `MaintainerConfig.agentic` carries one toggle per Phase 2 decision site.
- **Phase 2 LLM decision migrations** behind shadow:
  - PR readiness (#489) — `Readiness(verdict, confidence, blockers, summary)` via `structured_complete`; solves the solo-repo "reviews_approved ≥ 1" dead-end.
  - CI failure triage (#486) — `FailureTriage(category, confidence, is_transient, root_cause_hypothesis, minimal_reproduction, suggested_fix, files_to_touch)`; replaces the keyword ladder that returned mostly `UNKNOWN`.
  - Issue triage + dup detection (#488) — `IssueTriage(kind, severity, duplicate_of, staleness, ...)`; CVE regex kept as deterministic prefilter; Jaccard keyword overlap pre-selects dup candidates when embeddings aren't configured.
  - Webhook dispatch-guard self-echo (#487) — `DispatchVerdict(is_self_echo, is_human_intent, suggested_agent)`; regex prefilter short-circuits unambiguous cases to hold costs down.
  - Review-comment classification (#492) — `ReviewClassification(kind, severity, ...)`; severity propagates into the Copilot request-fix prompt.
  - Cascade redirection (#490) — `CascadeDecision(action, justification, confidence)` on `on_issue_closed_as_duplicate` + "PR body < 200 chars → close" heuristic; `parse_linked_issues` regex preserved.
  - Executor routing (#493) — `ExecutorRoute(path, reason, risk_tags)` shared across `pr_reviewer/routing.py` and `foundry/size_classifier.py` point systems.
  - Stuck-PR detection (#494) — `StuckVerdict(is_stuck, stuck_reason, recommended_action, explanation)`; `stuck_age_hours` kept as min-age prefilter; new `solo_repo_no_reviewer` + `self_approve_on_solo` signals for the solo-maintainer case.
  - Crystallizer category (#491) — `evolution/crystallizer.py::_infer_category` reuses `FailureTriage` behind `agentic.crystallizer_category`; `_CATEGORY_PATTERNS` retained until shadow data proves parity.

### Rollout pattern

Every A-migration lands **wrapped but not active**. Operators promote one site at a time: `off → shadow` (gather a week of disagreement data in `/api/admin/shadow/decisions`), then `off → enforce` once the disagreement rate is acceptable. Legacy heuristics stay live as the fallback on candidate errors.

### Test surface

Cumulative suite grew to **1456 passing tests / 1 skipped** across the release, with 31 (readiness), 23 (CI triage), 34 (issue triage), 37 (dispatch), 23 (review class), 26 (cascade), 31 (routing), 25 (stuck-PR), and decorator / adapter tests for shadow + bot-identity + doctor.

## [0.13.0] — 2026-04-22

Kicks off Phase 1 of the 2026-Q2 agentic-migration plan (see `docs/plans/`). Three foundational additions — prompt caching, a `structured_complete[T]` helper, and the first scope-gap + orchestrator bleeding fixes — plus Flux GitOps onboarding for the `bigboy` cluster.

### Added

- **Anthropic prompt caching** (#479) on both `AnthropicProvider` and `LiteLLMProvider`. Adds `cache_control: {"type": "ephemeral"}` to the system block on every completion; non-Claude models fail open to the un-cached path via a `_supports_prompt_cache(model)` substring gate. New Prometheus counters `caretaker_llm_cache_read_tokens_total` and `caretaker_llm_cache_creation_tokens_total`, labelled `{provider, model}`, let Grafana compute hit-ratio directly. Follow-up R2 spike queued to evaluate a second breakpoint on stable tool-loop context.
- **`ClaudeClient.structured_complete[T: BaseModel]`** (#477) — the helper every Phase 2 agentic migration depends on. Prepends `schema.model_json_schema()` to the system prompt, parses the response, and on `JSONDecodeError` or `ValidationError` re-asks once with the failure cue appended before raising `StructuredCompleteError(raw_text, validation_error)`. `LLMConfig.structured_output_retries` (default 1) tunes the retry budget. `pr_reviewer/inline_reviewer.py` migrated as the canary; its silent `verdict=COMMENT` downgrade on parse failure is gone.
- **403 scope-gap surfacing** (#480) — `ScopeGapTracker` (thread-safe, per-run) captures every "Resource not accessible by integration" / "Must have admin rights" 403 keyed on `(endpoint_template, http_method)`, maps endpoints to required scopes, and `publish_scope_gap_issue()` upserts a single `[caretaker] Workflow token is missing required scopes` issue per run (labels: `caretaker:scope-gap`, `maintainer:action-required`) with a concrete `permissions:` YAML snippet. New counter `caretaker_github_scope_gap_total{service, scope}`.
- **Orchestrator transient-error exit gate** (#481, T-M1) — run exits 0 when every collected `RunSummary.errors` entry falls into a known-transient bucket (403s, timeouts, upstream 5xx, empty-artifact) and measurable work was completed; emits `caretaker_orchestrator_soft_fail_total{category="transient"}`. `.github/workflows/maintainer.yml` `upload-artifact` steps now `continue-on-error: true` with `if-no-files-found: ignore`. Closes the "Unknown caretaker failure" self-heal storm observed fleet-wide.
- **Per-signature self-heal storm cap** (#481, T-M7) — cap key is now `(repo, error_signature, hour_window=floor(created_at/3600))` with a default `5/hour, 20/day` budget per key. Prevents one noisy signature from burning the global cap for every other sig or repo. Regression test simulates a 10-failure burst capped at 5.
- **Flux GitOps for `bigboy` AKS** (#478) — `k8s/flux/clusters/bigboy/caretaker.yaml` declares two Flux Kustomization CRs (`caretaker` + `caretaker-ingress`) that reconcile `k8s/apps/caretaker/` and `k8s/apps/caretaker-ingress/` respectively. Mirrors the pattern already in production for `Example-React-AI-Chat-App`. Resources stay in `infra/k8s/` as a single source of truth between hand-apply and GitOps.

### Fixed

- **Consumer-repo file writes always end in `\n`** (#476) — new `caretaker.util.text.ensure_trailing_newline` wired into both `foundry/tools._tool_write_file` (LLM-callable workspace writer) and `github_client/api.GitHubClient.create_or_update_file` (direct contents-API writer used by `docs_agent`). Closes the Copilot "add EOF newline" fan-out PR chain observed on `python_dsa`, `kubernetes-apply-vscode`, and `flashcards`.
- **Orchestrator-state comment upsert dedupe** (#481, T-M8) — `GitHubClient.upsert_issue_comment` now collects every marker-matching comment, edits the newest in place, and best-effort deletes older duplicates beyond `max_duplicates_to_retain` (default 2). Closes the 146-comment ballooning observed on `python_dsa` #23. Delete failures are logged, never raised.

## [0.12.1] — 2026-04-22

### Fixed

- **`FleetOAuthClientCache`** (#472): the OAuth2 client cache shipped in 0.12.0 lived in a pair of module-level globals. Any multi-tenant path — admin backend, tests constructing two `MaintainerConfig`s — would silently let the second config reuse the first's cached client. Extracted onto a per-owner class; `Orchestrator` owns its own instance and threads it through `emit_heartbeat`. Module singleton kept only for the legacy single-owner call path.
- **`datetime.utcnow()` sweep** (#471): replaced all call sites in `src/caretaker/` with `datetime.now(UTC)`. Python 3.12 deprecates `utcnow()`, and the resulting naive timestamps mixed unsafely with the tz-aware datetimes already used elsewhere. Added an AST-based regression test to prevent reintroductions.
- **PR-agent correctness batch** (#474): four small independent fixes — shadowed `pr_number` loop variable, `MERGE_READY` reported for human PRs even when auto-merge was disabled, duplicate-PR survivor policy disagreeing with the duplicate-issue sibling, and `_has_pending_task_comment` relying on implicit comment ordering.
- **Foundry tool-loop fallback** (#470): the `raw_message=None` defensive path built an OpenAI-shaped assistant message that would 400 on the next turn for Anthropic models. The path was papering over a provider-contract gap; dropped the fallback and require providers to populate `raw_message` on any tool-use turn.

### Docs

- **Plan of record** (#473): added `docs/plans/2026-Q2-agentic-migration.md` (master plan, 3 phases, 28 sub-agent tasks, parallelization map), `docs/plans/2026-04-22-fleet-audit.md` (operational audit of the five caretaker-topic consumer repos), and `docs/plans/2026-04-22-source-audit.md` (code-level bug list + agentic-migration candidates).

## [0.12.0] — 2026-04-22

### Added

- **OAuth2 `client_credentials` client** for service-to-service auth (`src/caretaker/auth/oauth_client.py`). `OAuth2ClientCredentials` posts to a token endpoint using `client_secret_basic`, caches the returned JWT in-process keyed by a 30-second skew buffer against the server-reported `expires_in`, and coalesces concurrent callers through an `asyncio.Lock` so a burst of requests triggers a single refresh. `build_client_from_env()` is the opt-in entry point — it returns `None` when the `OAUTH2_CLIENT_ID` / `OAUTH2_CLIENT_SECRET` / `OAUTH2_TOKEN_URL` trio is unset so callers fall through to their unauthenticated path with byte-identical behaviour.
- **`OAuth2ClientConfig`** on `FleetRegistryConfig.oauth2` (`config.py`). Off by default. When enabled alongside the fleet emitter, the heartbeat POST is decorated with `Authorization: Bearer <jwt>` from the cached client; HMAC (`CARETAKER_FLEET_SECRET`) and OAuth2 can be used together, and the backend can require either or both.
- **Module-level OAuth2 client cache in the fleet emitter**: the client is built once per process and reused across heartbeats, keyed on `(client_id, client_secret, token_url, scope_env, scope, timeout)` so a credential rotation invalidates the cache correctly without a process restart. A failed token fetch is logged at `WARNING` and swallowed — the emitter's fail-open contract is preserved so an auth-server outage never fails the run loop.

## [0.10.4] — 2026-04-21

### Added

- **PR Reviewer Agent** — dual-path automated code reviewer (enabled by default):
  - **Routing engine** (`pr_reviewer/routing.py`): scores each PR 0–100 from LOC, file count, sensitive-file patterns (workflows, auth, migrations, infra), cross-package breadth, and label signals. Score ≥ threshold (default 40) → complex path; score < threshold → fast path.
  - **Inline LLM reviewer** (`pr_reviewer/inline_reviewer.py`): fetches the unified diff, calls the configured LLM, returns a structured `ReviewResult` (summary + verdict + per-line inline comments); posts directly as a GitHub pull-request review event (APPROVE / COMMENT / REQUEST_CHANGES).
  - **Claude Code hand-off** (`pr_reviewer/claude_code_reviewer.py`): for complex PRs applies the `claude-code` trigger label and posts a structured `@claude` mention comment; the `anthropics/claude-code-action` workflow handles the full review asynchronously.
  - `PRReviewerConfig` in `config.py`: `enabled = true` (opt-out with `enabled = false`); `webhook_only = true` means the agent is a no-op during scheduled polling runs — zero extra GitHub API calls; `trigger_actions = ["opened"]` limits reviews to newly-opened PRs by default; full options: `routing_threshold`, `max_diff_lines`, `post_inline_comments`, `skip_draft`, `skip_labels`, `review_event`, `claude_code_label`/`claude_code_mention`.
  - New GitHub API methods: `get_pull_diff()` (vnd.github.diff accept header), `create_review()` (POST pulls/{n}/reviews with inline comments), `request_reviewers()` (POST pulls/{n}/requested_reviewers).
  - `PRReviewerAgent` registered in the agent registry under mode `pr-reviewer`; `pull_request` events in both `github_app/events.py` and `agents/_registry_data.py` now route to it.
  - `.github/CODEOWNERS` — `* @the-care-taker` so the bot appears in the PR reviewer picker.

- **M7 — Graph UI v2**: Five-tab admin graph page replacing the single explorer view.
  - **Explorer** tab: existing 2D/3D subgraph view with extended node-type filter list covering all M3–M6 node types (Repo, Comment, CheckRun, Executor, RunSummaryWeek, GlobalSkill, AgentCoreMemory).
  - **Timeline** tab: recharts run-history sparkline + scrollable run list on the left; click any run to load its N-hop Neo4j neighbourhood on the right. Depth selector (1–3).
  - **Goal Impact** tab: goal selector + ReactFlow DAG of the N-hop neighbourhood around the selected goal node.
  - **Causal Chain** tab: paginated causal-event list with source filter; click an event to split-view ancestor chain (left) and BFS descendants (right).
  - **Fleet** tab: ReactFlow force-layout where each node is a fleet repository (size and colour driven by `last_goal_health`); GlobalSkill shared-skill edges overlaid from the graph subgraph.
- **Memory page v2**: three-tab layout — KV Namespaces (existing), Core Memory (agent selector + live `AgentCoreMemory` graph-node display + recent-actions list), Skills (searchable skill table with confidence / run counts + skill drilldown showing causal-event graph neighbourhood).
- Shared `nodeColors.ts` constant covering all 17 node types (including the 7 added in M3–M6); consumed by Graph2DView, Graph3DView, and all new views — single source of truth for node colour palette.
- `Graph2DView` now accepts an optional `onNodeClick(nodeId, nodeType)` callback; used by upstream views that need node-click interactivity.

## [2026-W17] — 2026-04-21

- introduce agent protocol abstraction (AgentContext/AgentResult/BaseAgent) and improve registry type safety (#274)
- prevent duplicate @copilot task comments from concurrent workflow runs (#276)
- Resolve CodeQL `Analyze (python)` failure by removing conflicting advanced workflow (#279)
- Remove conflicting advanced CodeQL workflow causing `Analyze (python)` failures on `main` (#283)
- Self-heal: avoid env-noise “unknown error” titles by extracting from full job log (#286)
- Improve self-heal unknown failure extraction to avoid environment-noise issue titles (#288)
- [WIP] Fix caretaker self-heal for unknown failure (#290)
- Route Copilot wake-up comments through COPILOT_PAT identity (#292)
- Self-heal: extract actionable unknown-failure messages from Actions logs (#293)
- Add sync issue builder for client workflow/file reconciliation (#295)
- [WIP] Add installation of Claude agent from improvement repo (#297)
- address agent/orchestrator missed-goal patterns from workflow analysis (#298)
- Handle mixed naive/aware datetimes in orchestrator reconciliation (#300)
- handle 422 "Reference already exists" gracefully in DocsAgent (#304)
- handle 422 branch-already-exists gracefully (#306)
- [WIP] Fix unknown caretaker failure with exit code 1 (#308)
- handle 403 "not permitted to create PRs" as warning, not error (#310)
- Multi-layer dedup to prevent duplicate issues for same CI failures (#314)
- introduce goal-seeking subsystem with models and evaluation logic (#321)
- [WIP] Implement simple memory storage for caretaker (#323)
- Adjust image width in README (#324)
- Optimize GitHub API calls: PR-number fast path + in-process read cache (#326)
- [WIP] Update docs and readme to reflect current features (#328)
- implement workflow approval for action-required CI runs (#329)
- implement ReviewAgent (#330)
- Add Azure and MCP configuration options (#331)
- reconcile CHANGELOG — 2026-W16 (#332)
- implement authentication modes and update client logic (#333)
- add GitHub App integration scaffold: JWT signing, installation token minting, webhook verification, and github_app package (#334)
- add missing CheckStatus values and prevent docs agent 409 on stale branch (#336)
- [WIP] Update setup instructions for GitHub app and backend (#340)
- point release manifest URL at ianlintner/caretaker (#341)
- Update releases and docs to 0.5.2 (#343)
- [WIP] Create a plan for enhancing coding tasks with skills and agents (#345)
- skip changelog update if entry for the week already (#346)
- [WIP] Update versioning system to reflect latest releases (#348)
- Implement Azure backend with PostgreSQL, Redis, and MongoDB support (#349)
- Fix bump-version CI failure and duplicate Copilot PR creation (#351)
- Fix bump-version: use COPILOT_PAT for gh pr create in release.yml (#355)
- bump version to 0.6.4 (#357)
- update release workflow for improved version bump handling (#360)
- add `environment: pypi` to release publish job for trusted publishing (#363)
- bump version to 0.6.5 (#365)
- add PYPI_API_TOKEN fallback and skip_existing to release publish job (#367)
- add GitHub App authentication + credentials provider abstraction for GitHubClient (#369)
- bump version to 0.7.0 (#370)
- remove environment from PyPI OIDC claim to match trusted publisher config (#376)
- bump version to 0.7.1 (#377)
- add cooldown to reduce excessive CI job retriggers (#378)
- separate build output dir to prevent dist/ template files poisoning PyPI publish (#380)
- enhance PyPI publishing with token and OIDC support (#382)
- bump version to 0.7.2 (#383)
- correct twine upload path from dist/* to dist-packages/* (#387)
- introduce evolution/skill-learning subsystem with InsightStore and SkillCrystallizer for learning from PR outcomes (#389)
- bump version to 0.8.0 (#390)
- Enhance PR ownership management and PyPI publishing (#391)
- fix package name references: rename caretaker → caretaker-github throughout codebase (#392)
- build Foundry-backed coding executor (clone → LLM tool-loop → lint → commit → push) as an alternative to @copilot (#393)
- Enable Foundry executor (auto routing) (#394)
- bump version to 0.8.2 (#395)
- recognize AZURE_AI_API_KEY for Foundry availability (#396)
- Add Claude Code GitHub Workflow (#397)
- add 5 Opus-powered agents and refactor agents.py (#398)
- FastAPI admin dashboard + React SPA + Neo4j graph + AKS deploy workflow (#399)
- single caretaker status comment + configurable review gate (#403)
- Sprint 1: PR-workflow noise reduction (A1b, B1, C1+C2+C3, D1+D2) (#404)
- Sprint 2: comment idempotency, cooldowns, and self-heal storm cap (A3+A4, B2, C4) (#406)
- Sprint 3: stuck-PR age gate + retry-window reset (E1+E3) (#407)
- fall back to userinfo endpoint when ID token lacks email (#408)
- diag(admin): log token_data + ID-token claim keys on missing email (#409)
- handle GitHub 2500-comment limit on tracking issue; fix self-heal log auth (#411)
- expand B3 markers + F3 chain-audit backend (#420)
- dark mode + Grafana-inspired 2026 UI refresh (#421)
- use correct caretaker-github distribution name (#422)
- rename maintainer mode upgrade-only → upgrade (#423)
- opt-in cross-repo client roster (#425)
- label-based routing + expanded task allowlist (Phase 1) (#426)
- Phase 2 — Claude Code hand-off executor (#427)
- Phase 3 — on-demand Kubernetes worker (#428)
- wire MCP ServiceAccount + agent-worker manifest (#429)
- suspend agent-worker template Job + add e2e runbook (#430)
- install llm-multi extra so Foundry executor is reachable (#432)
- always ignore caretaker/pr-readiness in CI eval (self-deadlock) (#433)

## [Unreleased]

### Observability — Prometheus metrics + cluster scrape config

Completes the observability side of the paved-path rollout. Caretaker's
MCP backend now emits the RED-floor HTTP metrics, the outbound
`http_client_*` counterpart for every GitHub call, the `db_client_*`
family for Redis / MongoDB / Neo4j, and `worker_jobs_total` /
`worker_job_duration_seconds` for agent dispatch — all on the curated
§3 histogram buckets, all on a separate cluster-internal port so scrape
never contends with user traffic.

- New `prometheus-client>=0.20` + `prometheus-fastapi-instrumentator>=7.0`
  in the main dependency set (both are tiny, always-on — no extras
  flag needed).
- New `src/caretaker/observability/metrics.py` + templated-route HTTP
  middleware + `/metrics` sidecar on `:9090`.
- GitHub client, Neo4j, Mongo, Redis wrapped with `timed_op` emitting
  `http_client_*` / `db_client_*`. Registry wraps each agent dispatch
  with `worker_jobs_total` / `worker_job_duration_seconds`.
- K8s manifests publish named port `metrics` + `prometheus.io/*`
  scrape annotations + standard `app.kubernetes.io/*` labels.
- `docs/metrics-plan.md` captures the phase-1 audit + cardinality budget.
- 8 new tests in `tests/test_metrics.py` — RED counter increment,
  histogram bucket pinning, templated route regression guard,
  cardinality bound, `timed_op` success/failure outcomes.




### Memory graph — M8: OTel GenAI span instrumentation

Eighth milestone of the memory-graph plan (`docs/memory-graph-plan.md`
§6). Every agent dispatch now emits one `invoke_agent` OpenTelemetry
span following the April 2026 GenAI semantic conventions, and the
CausalEvent model carries the span provenance so "which span caused
this escalation" is a one-hop graph or trace-backend join.

- New optional `otel` extra in `pyproject.toml` pulling
  `opentelemetry-api`, `opentelemetry-sdk`, and
  `opentelemetry-exporter-otlp`. The default install is unchanged —
  the observability helpers degrade to no-op stubs when the SDK is
  missing or `OTEL_EXPORTER_OTLP_ENDPOINT` is unset.
- New `src/caretaker/observability/otel.py` exposing `init_tracing`,
  `agent_span`, and `current_span_ids`. `init_tracing` is idempotent
  and never raises; `agent_span` yields a `_NullSpan` stub when OTel
  is unavailable so call sites stay branch-free.
- `AgentRegistry.run_one` now wraps each `agent.execute` call in an
  `invoke_agent` span with `gen_ai.agent.name`,
  `gen_ai.operation.name`, and `caretaker.run_id` attributes. The MCP
  backend's FastAPI lifespan calls `init_tracing("caretaker-mcp")` on
  startup.
- `CausalEvent` gains optional `span_id` + `parent_span_id` fields,
  populated automatically by `extract_from_body` via
  `current_span_ids()`. `GraphBuilder.full_sync` writes both
  properties onto `:CausalEvent` nodes so the graph can join against
  the trace backend.
- New `docker-compose.override.yml` runs an Arize Phoenix sidecar
  (OTLP/gRPC on `:4317`, UI on `:6006`) for zero-cloud local trace
  viewing. `docs/memory-graph-plan.md` §6 documents the
  `docker compose up phoenix` + `OTEL_EXPORTER_OTLP_ENDPOINT` loop.
- 7 new tests in `tests/test_observability_otel.py` cover the no-op
  `init_tracing`, the `_NullSpan` fallback, `current_span_ids`
  outside a span, and the SDK-present init path (skipped when the
  `otel` extra is not installed).


### Memory graph — M2: missing edges + bitemporal edge properties

Second milestone of the memory-graph plan. The writer + builder now
emit richer, direction-aware edges stamped with bitemporal
properties, so "what state was PR #420 in when run R executed?" is a
one-hop query.

- New `RelType` values: `REFERENCES`, `RESOLVED_BY`, `EXECUTED`,
  `USED`, `VALIDATED_BY`, `AFFECTED`, `HANDLED_BY`.
- Every builder edge now carries `observed_at` + `valid_from`
  properties (and `valid_to` implicitly-null for still-current
  facts). The `GraphWriter` stamps `observed_at` automatically.
- `StateTracker._emit_run_graph` publishes the Run→Goal edge as
  `AFFECTED` with the run's goal-health score and escalation rate.
- `GraphBuilder.full_sync` adds PR→Issue `REFERENCES` and Issue→PR
  `RESOLVED_BY` derived from `TrackedIssue.assigned_pr`, plus
  Run→Agent `EXECUTED` edges derived from `RunSummary.mode` and
  Run→Goal `AFFECTED` with the run's score. Run ids are now
  `run:<run_at_iso>` so nodes survive the rolling 20-run window.
- Synthetic `goal:overall` aggregate node is merged by both the
  builder and the live writer, so Run→Goal edges can MATCH both
  endpoints regardless of ordering.
- 5 new tests in `tests/test_graph_builder_m2.py` cover PR↔Issue
  edge pairs, mode-based EXECUTED fan-out, AFFECTED score payload,
  synthetic Goal aggregate, and full-mode dispatch fan-out.

### Memory graph — M1: event-driven GraphWriter

First milestone of the memory-graph plan (`docs/memory-graph-plan.md`).
Turns the Neo4j graph from a 60-second dashboard replica into a live
system of record that agent call sites write to as events happen.

- New `src/caretaker/graph/writer.py`: `GraphWriter` singleton with a
  thread-safe enqueue path and an asyncio background drain. Sync
  `record_node` / `record_edge` helpers, bitemporal `observed_at`
  stamping, bounded retries (3 attempts) before dropping a batch so a
  degraded Neo4j cluster cannot stall the orchestrator hot path. A
  daily full-sync via `GraphBuilder.full_sync` remains the
  reconciliation fallback.
- `StateTracker.save` publishes a `Run` node + `Run→Goal
  CONTRIBUTES_TO` edge with the run's goal-health score.
- `InsightStore.record_success` / `record_failure` publish the latest
  `Skill` counters so confidence drift is queryable without waiting
  for a full_sync.
- `admin.state_loader.build_refresh_task` now holds a persistent
  `GraphStore` across ticks and calls `writer.configure(...)` +
  `writer.start()` the first time the refresh loop wakes up when
  `NEO4J_URL` is set.
- 8 new unit tests in `tests/test_graph_writer.py` exercise enqueue,
  drain, retry, timeout, disable and the process-wide singleton —
  using a fake `GraphStore` so no Neo4j is required.

### Custom coding agent — Phase 3: on-demand Kubernetes worker

Third phase of the custom-coding-agent plan. The MCP backend can now
spawn a short-lived `batch/v1 Job` per coding task, running caretaker's
custom executor in an isolated pod on the existing AKS cluster rather
than competing with the orchestrator's GitHub Actions minutes.

- New `K8sAgentLauncher` in `src/caretaker/k8s_worker/launcher.py`:
  pure-function `build_job_manifest()` synthesises the Job spec; the
  launcher calls `BatchV1Api.create_namespaced_job` and records a
  Redis-backed dedupe pointer so a retried submit inside the TTL
  window returns the existing Job name instead of spawning a second
  pod.
- New admin endpoints (mounted when `executor.k8s_worker.enabled`):
  * `POST /api/admin/agent-tasks {repo, issue_number, task_type, image?}`
  * `GET  /api/admin/agent-tasks?limit=50`
  Both gated behind the existing OIDC session.
- New `K8sAgentWorkerConfig` on `MaintainerConfig.executor` — all
  knobs (namespace, image, service account, TTL, deadlines, dedupe
  window) off by default.
- `kubernetes` Python client added as an **optional** dependency in
  the new `k8s-worker` extras group. When not installed, the launcher
  raises `K8sLauncherError` instead of `ImportError` so the admin
  endpoints return a structured 503.
- Manifest skeleton `infra/k8s/caretaker-agent-worker.yaml` (shipped
  inert in Phase 1) is now the template the launcher clones per
  dispatch.
- Plan doc Phase-3 section updated to reflect shipped state.
- Tests: 17 new cases covering config defaults, Job name and manifest
  synthesis, dedupe, API dispatch happy-path, 400/503 error paths.
  Full pytest 907 passed.

### Custom coding agent — Phase 2: Claude Code hand-off executor

Second phase of the custom-coding-agent plan. `ExecutorDispatcher` now
routes to a second non-Copilot executor that hands tasks off to the
upstream `anthropics/claude-code-action` workflow.

- New `ClaudeCodeExecutor` (`src/caretaker/claude_code_executor.py`).
  Conforms to the same `async run(task, pr) -> ExecutorResult` shape
  as `FoundryExecutor` so the dispatcher treats it as a peer.
  Dispatch model: post `@claude` mention comment + apply trigger
  label; upstream workflow produces the fix asynchronously; existing
  `<!-- caretaker:result -->` markers close the loop.
- New `ClaudeCodeExecutorConfig` on `MaintainerConfig.executor`:
  `enabled`, `trigger_label` (default `claude-code`), `mention`
  (default `@claude`), `max_attempts` (default 2). Feature is off by
  default; `executor.provider` extended to `copilot | foundry |
  claude_code | auto`.
- Dispatcher adds `RouteOutcome.CLAUDE_CODE`. `provider=auto` now
  tries Claude Code when Foundry is ineligible but Claude Code is
  enabled, before falling to Copilot. `agent:custom` label honours
  whichever custom executor is currently active.
- Attempt cap prevents ping-pong: executor counts prior hand-off
  comments (marker `<!-- caretaker:claude-code-handoff -->`); beyond
  `max_attempts` it escalates to Copilot.
- Tests: 12 new cases, full pytest 890 passed.
- Plan doc Phase-2 section updated to reflect shipped state.

### Fleet Registry — opt-in cross-repo client roster

New opt-in telemetry surface so an operator can see every
caretaker-managed repository in one dashboard without running an
org-wide GitHub crawl.

- **Emitter** — at the end of each successful `caretaker run`, if
  `fleet_registry.enabled: true` and `fleet_registry.endpoint` is set,
  the orchestrator POSTs a small JSON heartbeat with the repo slug,
  caretaker version, run mode, enabled agents, and 20 curated
  `RunSummary` counters. HMAC-SHA256 signed with the optional
  `CARETAKER_FLEET_SECRET` shared secret. Fail-open: network /
  configuration errors log a warning and never fail the run.
- **Backend** — new `POST /api/fleet/heartbeat` (unauthenticated, HMAC
  verified when the backend also has `CARETAKER_FLEET_SECRET` set) and
  `GET /api/admin/fleet`, `/api/admin/fleet/summary`,
  `/api/admin/fleet/{owner}/{repo}` behind the existing OIDC gate.
  First cut uses an async-safe in-memory `FleetRegistryStore` —
  persistence can plug in later without changing the API.
- **Dashboard** — new `/fleet` route with StatPanel strip (total,
  stale >7d, version mix, opt-in status) + hairline DataTable of every
  known client.
- **Opt-in, off by default.** No heartbeat unless both `enabled` and
  `endpoint` are set; caretaker never phones home.
- **Docs** — `docs/fleet-registry.md` walks through configuration,
  operation, payload shape, and design notes.

### Sprint B3 + F3 — causal-chain audit trail

Every caretaker-authored write now carries a hidden `<!-- caretaker:causal
id=... source=... [parent=...] -->` marker. The admin dashboard harvests
those markers into an in-memory store and exposes chain-walking
endpoints so we can answer questions like "this self-heal issue was
filed — what sequence of runs produced it?"

- **B3 expansion** — causal markers now injected into PR-agent
  escalation comments, issue-agent dispatch bodies/comments, Charlie
  close comments (duplicate + stale for both issues and PRs),
  escalation-agent digests, and state-tracker's orchestrator-state +
  run-history comments. Parent causal id is inherited from the source
  issue/PR body where applicable, so chains stitch across runs.
- **F3-1/2/3** — new `caretaker.causal_chain` module with
  `CausalEvent`, `CausalEventRef`, `Chain`, `walk_chain()` (root-first,
  cycle-safe, depth-bounded) and `descendants()` (BFS).
- **F3-4/5** — `CausalEventStore` hydrated every 60s by the admin
  refresh loop (scans tracked issues/PRs + the orchestrator
  tracking-issue comment stream). New `/api/admin/causal` endpoints:
  list (paged, filter by source), fetch chain for an event, fetch
  descendants.
- **F3-6** — Neo4j sync persists `CausalEvent` nodes and `CAUSED_BY`
  edges so graph queries can traverse provenance alongside existing
  PR/Issue/Agent/Run nodes.
- New `GitHubClient.get_issue(owner, repo, number)` helper (needed by
  the causal store's per-issue fetch during refresh).
- Causal marker regex now accepts colons in the `source=` value so
  composite sources like `issue-agent:dispatch` and
  `pr-agent:escalation` round-trip cleanly.

Status comments intentionally skipped: `upsert_status_comment` uses a
strict body-equality idempotency check, so a fresh run-scoped marker
on every cycle would break the skip-if-unchanged path.

## [0.10.0] - 2026-04-19

PR-workflow noise reduction sweep. Three-sprint plan addressing the 60-day audit findings on caretaker self-instance, rust-oauth2-server, and portfolio.

### Sprint 1 — kill the noise loops (#404)

- One-shot legacy comment compaction: pre-#403 PRs with stale ownership:claim / readiness:update duplicates get collapsed to the single status comment on next cycle (A1b)
- Dispatch-guard tightening: skip caretaker-marker comments regardless of authoring identity; expand bot-actor allowlist; skip Copilot reviewer reviews; downstream template gets a minimal version (B1)
- `is_actionable_conclusion()` helper + `NON_ACTIONABLE_CONCLUSIONS` set — refuse to triage cancelled/skipped/neutral check runs (C1+C2)
- `_handle_ci_fix` skips `@copilot` task posting when failure is UNKNOWN with empty error logs (C3)
- Upgrade-issue marker dedupe: `<!-- caretaker:upgrade target=X.Y.Z -->` body marker with title-substring fallback + backfill, fixes the rust-oauth2-server #118/#121/#126/#129/#153 v0.5.0 dupe pattern (D1)
- Upgrade-PR dedupe: when multiple open PRs race the same upgrade target, keep the newest and close the rest with a `Superseded by #N` comment. Addresses portfolio #144/#146 racing pattern (D2)

### Sprint 2 — comment idempotency, cooldowns, storm cap (#406)

- `upsert_issue_comment(marker, body, *, legacy_markers, min_seconds_between_updates)` lifted into `GitHubClient` (A3)
- `comment_cap_per_issue` (default 25) on `add_issue_comment` for caretaker-marker bodies (A4)
- Orchestrator state and rolling run-history switched to upsert (was append-per-run; portfolio #121 hit 110 bot comments)
- Escalation comments upserted by marker with 1h cooldown — no more 14-dupe escalation pings (portfolio #148)
- Self-heal storm cap: max 5 issues/hour, 20/day. Catches the F1 retry-storm pattern (108 PRs in 90 min on 2026-04-14) (C4)
- Per-event-class workflow concurrency: only cancel on `pull_request` events; serialize the rest. Reduces the 67% cancelled-run rate (B2)

### Sprint 3 — stuck-PR age gate + retry-window reset (#407)

- `stuck_age_hours` config (default 24, 0 disables) on PRAgentConfig — escalate PRs open longer than threshold without human approval. Catches portfolio #4 (10d) / #28 (7d) abandonment (E1)
- Wire previously-unused `retry_window_hours` (default 24) — reset `copilot_attempts` when last attempt aged out (E3)
- New `TrackedPR.last_copilot_attempt_at` timestamp field
- Documentation note on unused `auto_approve_copilot` config

### Test coverage

- 769 tests pass (was 661 baseline at start of Sprint 1)
- New test files: `tests/test_state_tracker.py`
- New test classes: `TestCompactLegacyComments`, `TestIsActionableConclusion`, `TestUpgradeIssueMarkerDedupe`, `TestSelfHealStormCap`, `TestStuckPRAgeGate`, `TestRetryWindowHours`, plus upsert/cap/cooldown test cases on `tests/test_github_client_api.py`

## [2026-W16] - 2026-04-16

- Add Charlie agent for janitorial cleanup of caretaker-managed issues and PRs (#237)
- Follow-up: add CI backlog guard (close_managed_prs_on_backlog) and fix PR agent CI triage (#238)
- Handle 403 rate-limit errors and guard state load against unhandled crash (#244)
- Docs build no longer fails on configure-pages API errors (#256)
- Remove committed site/ build artifacts; add CodeQL exclusion config (#259)
- Guard FailureType → TaskType conversion against unmapped values (#263)
- Treat 405/409/422 merge rejections as waiting, not errors (#265)
- Fix CI failure on main for Analyze (javascript-typescript) (#268)
- Replace dynamic CodeQL javascript-typescript scan with explicit Python-only workflow (#269)
- Group related issues/PRs by workflow run_id (#272)
- Refactor: introduce agent protocol abstraction (BaseAgent, AgentContext, AgentResult) with registry type-safety improvements (#274)
- Prevent duplicate @copilot task comments from concurrent workflow runs (#276)
- Resolve CodeQL `Analyze (python)` failure by removing conflicting advanced workflow (#279)
- Remove conflicting advanced CodeQL workflow causing `Analyze (python)` failures on `main` (#283)
- Self-heal: avoid env-noise "unknown error" titles by extracting from full job log (#286)
- Improve self-heal unknown failure extraction to avoid environment-noise issue titles (#288)
- Fix caretaker self-heal for unknown failure (#290)
- Route Copilot wake-up comments through COPILOT_PAT identity (#292)
- Self-heal: extract actionable unknown-failure messages from Actions logs (#293)
- Add sync issue builder for client workflow/file reconciliation (#295)
- Add installation of Claude agent from improvement repo (#297)
- Address agent/orchestrator missed-goal patterns from workflow analysis (#298)
- Handle mixed naive/aware datetimes in orchestrator reconciliation (#300)
- Handle 422 "Reference already exists" gracefully in DocsAgent (#304)
- Handle 422 branch-already-exists gracefully (#306)
- Fix unknown caretaker failure with exit code 1 (#308)
- Handle 403 "not permitted to create PRs" as warning, not error (#310)
- Multi-layer dedup to prevent duplicate issues for same CI failures (#314)
- Introduce goal-seeking subsystem with models and evaluation logic (#321)
- Implement simple memory storage for caretaker (#323)
- Adjust image width in README (#324)
- Optimize GitHub API calls: PR-number fast path + in-process read cache (#326)
- Update docs and readme to reflect current features (#328)
- Implement workflow approval for action-required CI runs (#329)
- Implement ReviewAgent (#330)
- Add Azure and MCP configuration options (#331)

## [0.5.2] - Current

### Core Agents

- **PR Agent**: Full-lifecycle PR management with CI triage, auto-merge, retry logic, and review analysis
- **Issue Agent**: Intelligent issue triage with auto-assignment, lifecycle tracking, and escalation
- **DevOps Agent**: Default-branch CI monitoring with automated fix issue creation and deduplication
- **Self-Heal Agent**: Caretaker self-diagnosis with upstream bug reporting and cooldown management
- **Security Agent**: Multi-source security triage (Dependabot, code scanning, secret scanning) with severity filtering
- **Dependency Agent**: Dependabot PR review with smart auto-merge strategies and weekly digests
- **Docs Agent**: Changelog reconciliation from merged PRs with configurable lookback and branch management
- **Charlie Agent**: Operational clutter cleanup for caretaker-managed work with 14-day default window
- **Stale Agent**: Comprehensive stale issue/PR closure with merged branch deletion and exempt labels
- **Upgrade Agent**: Multi-strategy caretaker version upgrades (auto-minor, auto-patch, latest, pinned) with preview channel support
- **Escalation Agent**: Human escalation digest aggregation with configurable notification
- **Review Agent**: Automated code review dispatch with configurable triggers

### Recent Changes (since 0.5.0)

- Add Charlie agent for janitorial cleanup of caretaker-managed issues and PRs (#237)
- Add CI backlog guard (close_managed_prs_on_backlog) and fix PR agent CI triage (#238)
- Handle 403 rate-limit errors and guard state load against unhandled crash (#244)
- Docs build no longer fails on configure-pages API errors (#256)
- Remove committed site/ build artifacts; add CodeQL exclusion config (#259)
- Guard FailureType → TaskType conversion against unmapped values (#263)
- Treat 405/409/422 merge rejections as waiting, not errors (#265)
- Replace dynamic CodeQL javascript-typescript scan with explicit Python-only workflow (#269)
- Group related issues/PRs by workflow run_id (#272)
- Introduce agent protocol abstraction (BaseAgent, AgentContext, AgentResult) with registry type-safety improvements (#274)
- Prevent duplicate @copilot task comments from concurrent workflow runs (#276)
- Self-heal: avoid env-noise "unknown error" titles by extracting from full job log (#286, #288, #290, #293)
- Route Copilot wake-up comments through COPILOT_PAT identity (#292)
- Add sync issue builder for client workflow/file reconciliation (#295)
- Handle mixed naive/aware datetimes in orchestrator reconciliation (#300)
- Handle 422 "Reference already exists" gracefully in DocsAgent (#304, #306)
- Handle 403 "not permitted to create PRs" as warning, not error (#310)
- Multi-layer dedup to prevent duplicate issues for same CI failures (#314)
- Introduce goal-seeking subsystem with models and evaluation logic (#321)
- Implement simple memory storage for caretaker (#323)
- Optimize GitHub API calls: PR-number fast path + in-process read cache (#326)
- Update docs and readme to reflect current features (#328)
- Implement workflow approval for action-required CI runs (#329)
- Implement ReviewAgent (#330)
- Add Azure and MCP configuration options (#331)

## [0.5.0]

### Core Agents

- **PR Agent**: Full-lifecycle PR management with CI triage, auto-merge, retry logic, and review analysis
- **Issue Agent**: Intelligent issue triage with auto-assignment, lifecycle tracking, and escalation
- **DevOps Agent**: Default-branch CI monitoring with automated fix issue creation and deduplication
- **Self-Heal Agent**: Caretaker self-diagnosis with upstream bug reporting and cooldown management
- **Security Agent**: Multi-source security triage (Dependabot, code scanning, secret scanning) with severity filtering
- **Dependency Agent**: Dependabot PR review with smart auto-merge strategies and weekly digests
- **Docs Agent**: Changelog reconciliation from merged PRs with configurable lookback and branch management
- **Charlie Agent**: Operational clutter cleanup for caretaker-managed work with 14-day default window
- **Stale Agent**: Comprehensive stale issue/PR closure with merged branch deletion and exempt labels
- **Upgrade Agent**: Multi-strategy caretaker version upgrades (auto-minor, auto-patch, latest, pinned) with preview channel support
- **Escalation Agent**: Human escalation digest aggregation with configurable notification

### Advanced Features

- **Goal Engine** (Experimental): Quantitative goal-based agent dispatch
  - CI health, PR lifecycle, security posture, and self-health goals
  - 0.0–1.0 scoring with satisfaction and critical thresholds
  - Divergence detection and trend analysis
  - Optional goal-driven agent reordering
  - Per-goal history tracking for escalation
- **Memory Store**: Persistent SQLite-backed agent memory
  - Namespaced key-value storage
  - Cross-run deduplication signatures
  - Automatic JSON snapshot generation
  - Bounded storage with configurable limits
  - Cooldown timer persistence

### GitHub Client Optimizations

- In-process read cache for GET requests
- PR-number fast path from webhook events
- Async/await HTTP calls via httpx
- Typed Pydantic models for all responses
- Automatic pagination handling

### Configuration System

- Strict validation with `extra = forbid` (fail fast on unknown keys)
- Full Pydantic-based config models with defaults
- JSON schema at `schema/config.v1.schema.json`
- CLI validation command: `caretaker validate-config`
- Comprehensive LLM feature toggles (Claude integration)
- Per-agent enable/disable switches
- Extensive auto-merge, retry, and cooldown tuning

### State Management

- Issue-backed persistence via hidden marker blocks
- Indented JSON for readability in GitHub comments
- Tracked PR states: discovered → CI pending/failing/passing → review → merge ready → merged
- Tracked issue states: new → triaged → assigned → in progress → PR opened → completed
- Per-agent run summaries with error tracking
- Goal history snapshots (when goal engine enabled)

### LLM Integration

- Optional Claude integration via `ANTHROPIC_API_KEY`
- CI log analysis for long, noisy logs
- Architectural review comment understanding
- Issue decomposition for complex bugs
- Upgrade impact analysis
- Per-feature toggles in config

### Consumer Setup

- Zero-config onboarding via `SETUP_AGENT.md` + `@copilot`
- Template-based workflow, config, and agent persona generation
- Copilot project memory integration
- Version pinning via `.github/maintainer/.version`
- `COPILOT_PAT` for write-capable Copilot handoffs

### Developer Experience

- CLI entrypoint: `caretaker run`
- Dry-run mode for testing
- Comprehensive test suite (300+ tests) with pytest
- Strict ruff linting and mypy type checking
- MkDocs-based documentation site with Material theme
- CI pipeline with coverage reporting

### Notable Implementation Details

- Agent registry pattern with mode-based dispatch
- Event routing with fast-path PR extraction
- Three-layer deduplication: state sigs, run_id tracking, cooldown timers
- UTC datetime normalization for age calculations
- Workflow-specific event payloads (`_pr_number`, `_head_branch`)
- Error message parsing for CI failures (timestamp stripping, exit code filtering)
