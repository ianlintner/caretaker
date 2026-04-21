# Changelog

All notable changes to this project will be documented in this file.

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
