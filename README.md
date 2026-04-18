# Caretaker

Autonomous GitHub repository management powered by Copilot and github app.

Documentation: https://ianlintner.github.io/caretaker/

<img width="450" alt="Gemini_Generated_Image_544abh544abh544a" src="https://github.com/user-attachments/assets/abd77a15-aa7f-41d3-b56c-ff8ec7f89542" />

**One issue. No CLI. No tooling.** Paste a setup issue into your repo, assign it to `@copilot`, walk away. Your repo is now autonomously maintained.

---

## How It Works

1. **You** paste a setup issue into your repo and assign it to `@copilot`
2. **Copilot** reads our [SETUP_AGENT.md](setup-templates/SETUP_AGENT.md), analyzes your repo, and opens a PR with everything configured
3. **You** merge the PR
4. **The orchestrator** runs daily via GitHub Actions, managing PRs, issues, and upgrades

The orchestrator uses Copilot as its execution engine — it observes your repo state, decides what needs to happen, and delegates code changes to Copilot via structured comments.

---

## Setup

### 1. Create a new issue in your repo:

> **Tip:** Visit the [Getting Started docs](https://ianlintner.github.io/caretaker/getting-started/) and use the **copy** button on the code block below to copy the issue template in one click.

```markdown
## Setup Caretaker

@copilot Please set up the caretaker system for this repository.

### Instructions

1. Read the setup guide at:
   https://github.com/ianlintner/caretaker/blob/main/setup-templates/SETUP_AGENT.md

2. Follow the instructions in that guide exactly.

3. After creating all files, open a single PR with the changes.
   Title: "chore: setup caretaker"

### Context

This repo uses the caretaker system for automated repo management.
See: https://github.com/ianlintner/caretaker
```

### 2. Assign the issue to `@copilot`

### 3. Review and merge the PR that Copilot opens

### 4. Add `COPILOT_PAT` from a write-capable user for Copilot hand-offs, and `ANTHROPIC_API_KEY` for enhanced AI features

`COPILOT_PAT` should be a fine-grained PAT that belongs to a real user or machine user with write access to the repository.
Caretaker uses that token for:

- API-based assignment of issues to GitHub Copilot
- PR comments that `@copilot` must see as coming from a write-capable identity rather than `github-actions[bot]`

---

## What Gets Installed

After setup, your repo has:

```
.github/
  copilot-instructions.md         ← Copilot project memory (appended)
  agents/
    maintainer-pr.md              ← PR agent persona
    maintainer-issue.md           ← Issue agent persona
    maintainer-upgrade.md         ← Upgrade agent persona
  maintainer/
    config.yml                    ← Repo-specific settings
    .version                      ← Pinned version
  workflows/
    maintainer.yml                ← Orchestrator workflow
```

No Python. No Node. No vendored code. Just config and Copilot instructions.

---

## Features

### Core Agents

#### PR Agent
- Monitors all open PRs in real-time
- Detects and triages CI failures (test, lint, build, type errors)
- Requests fixes from Copilot via structured comments
- Retry loop with escalation after max attempts
- Auto-merge for Copilot, Dependabot, and human PRs (configurable)
- Handles flaky test detection and CI re-runs
- Review state analysis and auto-approval (configurable)

#### Issue Agent
- Triages incoming issues (bug, feature, question, duplicate, stale)
- Dispatches implementable issues to Copilot
- Tracks issue → PR → merge lifecycle
- Auto-closes answered questions and stale issues (configurable)
- Escalates complex issues to repo owners

#### DevOps Agent
- Monitors default-branch CI failures
- Automatically creates fix issues for build/test failures
- Deduplicates similar issues with cooldown periods
- Assigns work to Copilot for resolution

#### Self-Heal Agent
- Detects caretaker's own workflow failures
- Creates self-diagnosis issues
- Reports bugs to upstream caretaker repository (configurable)
- Ensures the system can maintain itself

#### Security Agent
- Triages Dependabot alerts
- Monitors code scanning findings
- Tracks secret scanning alerts
- Filters by severity thresholds
- Creates remediation issues with context

#### Dependency Agent
- Reviews Dependabot PRs
- Auto-merges patch and minor updates (configurable)
- Posts dependency update digests
- Smart merge strategies by update type

#### Docs Agent
- Reconciles merged PRs into changelog updates
- Maintains documentation freshness
- Configurable lookback period
- Optional README updates

#### Charlie Agent
- Cleans up duplicate caretaker-managed issues and PRs
- Closes abandoned work after 14-day default window
- Prevents operational clutter accumulation
- Exempt label support for critical work

#### Stale Agent
- Warns and closes stale issues and PRs (60+ days default)
- Deletes merged branches automatically
- Configurable stale thresholds
- Exempt labels for pinned or security work

#### Escalation Agent
- Creates human escalation digest issues
- Aggregates work requiring maintainer attention
- Configurable targets and notification
- Tracks escalation age and priority

#### Upgrade Agent
- Detects new caretaker releases
- Creates upgrade issues for Copilot execution
- Supports multiple strategies: auto-minor, auto-patch, latest, pinned
- Handles breaking vs. non-breaking upgrades
- Version pinning via `.version` file
- Preview channel support

### Advanced Features

#### Goal Engine (Experimental)
- Quantitative goal-based agent dispatch
- Measures repository health across dimensions:
  - CI health (green builds on main and PRs)
  - PR lifecycle velocity
  - Security posture
  - Self-health monitoring
- Scores each goal from 0.0 (unmet) to 1.0 (satisfied)
- Prioritizes agents based on goal impact
- Detects divergence and critical states
- Tracks goal history for trend analysis

#### Memory Store
- Disk-backed SQLite storage for agent memory
- Persistent deduplication across runs
- Namespaced memory for different agent concerns
- Automatic snapshot generation for auditing
- Bounded storage with configurable limits

### Optional: Claude Integration

Add `ANTHROPIC_API_KEY` to unlock enhanced AI features:

- **CI log analysis** — better at parsing long, noisy logs
- **Architectural review** — understands complex code review comments
- **Issue decomposition** — breaks down multi-faceted bugs
- **Upgrade impact analysis** — assesses breaking change risk

---

## Configuration

See [setup-templates/templates/config-default.yml](setup-templates/templates/config-default.yml) for the full config schema.

Key settings:

```yaml
pr_agent:
  auto_merge:
    copilot_prs: true # Auto-merge Copilot PRs
    dependabot_prs: true # Auto-merge dependency updates
  copilot:
    max_retries: 2 # Fix attempts before escalation

issue_agent:
  auto_assign_bugs: true # Auto-assign simple bugs to Copilot
  auto_assign_features: false

devops_agent:
  target_branch: main # Monitor default branch CI
  max_issues_per_run: 3 # Prevent issue spam
  dedup_open_issues: true

security_agent:
  min_severity: medium # Filter by severity
  include_dependabot: true
  include_code_scanning: true
  include_secret_scanning: true

dependency_agent:
  auto_merge_patch: true
  auto_merge_minor: true
  post_digest: true

charlie_agent:
  stale_days: 14 # Short janitorial window for caretaker-managed work
  close_duplicate_issues: true
  close_duplicate_prs: true

stale_agent:
  stale_days: 60 # General stale threshold
  close_after: 14
  delete_merged_branches: true

upgrade_agent:
  strategy: auto-minor # auto-minor | auto-patch | latest | pinned
  channel: stable # stable | preview

goal_engine:
  enabled: false # Experimental: goal-driven dispatch
  goal_driven_dispatch: false # Reorder agents by goal impact
  divergence_threshold: 3 # Runs before triggering alerts

memory_store:
  enabled: true # Persistent agent memory
  db_path: .caretaker-memory.db
  max_entries_per_namespace: 1000
```

---

## Architecture

```
Orchestrator (Python, runs in GitHub Actions)
  │
  ├── Reads config.yml
  ├── Reads repo state (open PRs, issues, CI status)
  ├── Decides what needs to happen
  │
  ├── For code changes → creates/updates issues → assigns to @copilot
  ├── For PR fixes → posts structured comments as the `COPILOT_PAT` identity → @mentions copilot
  └── For escalation → labels + tags repo owner
```

The orchestrator **never writes code**. It manages Copilot, which does.

---

## Development

```bash
# Clone and install
git clone https://github.com/ianlintner/caretaker.git
cd caretaker
pip install -e ".[dev]"

# Run tests
pytest tests/ -v

# Lint
ruff check src/ tests/
ruff format --check src/ tests/

# Type check
mypy src/
```

---

## License

MIT
