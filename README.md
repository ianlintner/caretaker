# Caretaker

Autonomous GitHub repository management powered by Copilot.

Documentation: https://ianlintner.github.io/caretaker/

**One issue. No CLI. No tooling.** Paste a setup issue into your repo, assign it to `@copilot`, walk away. Your repo is now autonomously maintained.

---

## How It Works

1. **You** paste a setup issue into your repo and assign it to `@copilot`
2. **Copilot** reads our [SETUP_AGENT.md](dist/SETUP_AGENT.md), analyzes your repo, and opens a PR with everything configured
3. **You** merge the PR
4. **The orchestrator** runs weekly via GitHub Actions, managing PRs, issues, and upgrades

The orchestrator uses Copilot as its execution engine — it observes your repo state, decides what needs to happen, and delegates code changes to Copilot via structured comments.

---

## Setup

### 1. Create a new issue in your repo:

```markdown
## Setup Caretaker

@copilot Please set up the caretaker system for this repository.

### Instructions

1. Read the setup guide at:
   https://github.com/your-org/caretaker/blob/main/dist/SETUP_AGENT.md

2. Follow the instructions in that guide exactly.

3. After creating all files, open a single PR with the changes.
   Title: "chore: setup caretaker"

### Context

This repo uses the caretaker system for automated repo management.
See: https://github.com/your-org/caretaker
```

### 2. Assign the issue to `@copilot`

### 3. Review and merge the PR that Copilot opens

### 4. (Optional) Add `ANTHROPIC_API_KEY` to repo secrets for enhanced AI features

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

### PR Agent (Phase 1)

- Monitors all open PRs
- Detects CI failures and triages them (test, lint, build, type errors)
- Requests fixes from Copilot via structured comments
- Retry loop with escalation after max attempts
- Auto-merge for Copilot and Dependabot PRs (configurable)
- Handles flaky test detection and CI re-runs

### Issue Agent

- Triages incoming issues (bug, feature, question, duplicate, stale)
- Dispatches implementable issues to Copilot
- Tracks issue → PR → merge lifecycle
- Auto-closes answered questions (configurable)
- Escalates complex issues to repo owner

### Upgrade Agent

- Checks for new caretaker releases
- Creates upgrade issues for Copilot to execute
- Handles breaking vs. non-breaking upgrades
- Version pinning via `.version` file

### Optional: Claude Integration

Add `ANTHROPIC_API_KEY` to unlock:

- CI log analysis (better at parsing long logs)
- Architectural review comment understanding
- Issue decomposition for complex bugs
- Upgrade impact analysis

---

## Configuration

See [dist/templates/config-default.yml](dist/templates/config-default.yml) for the full config schema.

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

upgrade_agent:
  strategy: auto-minor # auto-minor | auto-patch | latest | pinned
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
  ├── For PR fixes → posts structured comments → @mentions copilot
  └── For escalation → labels + tags repo owner
```

The orchestrator **never writes code**. It manages Copilot, which does.

---

## Development

```bash
# Clone and install
git clone https://github.com/your-org/caretaker.git
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
