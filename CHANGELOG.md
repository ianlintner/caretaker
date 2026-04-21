# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

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
