# Changelog

All notable changes to this project will be documented in this file.

## [0.1.0] - Current

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
