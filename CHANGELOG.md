# Changelog

All notable changes to this project will be documented in this file.

## [2026-W16] — 2026-04-17

- add Charlie agent for janitorial cleanup of caretaker-managed issues and PRs (#237)
- Followup (#238)
- handle 403 rate-limit errors and guard state load against unhandled crash (#244)
- docs build no longer fails on configure-pages API errors (#256)
- Remove committed site/ build artifacts; add CodeQL exclusion config (#259)
- guard FailureType → TaskType conversion against unmapped values (#263)
- treat 405/409/422 merge rejections as waiting, not errors (#265)
- [WIP] Fix CI failure on main for Analyze (javascript-typescript) (#268)
- replace dynamic CodeQL javascript-typescript scan with explicit Python-only workflow (#269)
- group related issues/PRs by workflow run_id (#272)
- Agentic (#274)
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
- GitHub app (#334)
- add missing CheckStatus values and prevent docs agent 409 on stale branch (#336)
- [WIP] Update setup instructions for GitHub app and backend (#340)
- point release manifest URL at ianlintner/caretaker (#341)
- Update releases and docs to 0.5.2 (#343)
- [WIP] Create a plan for enhancing coding tasks with skills and agents (#345)

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
