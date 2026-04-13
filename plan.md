# Caretaker — Consolidated Architecture Plan

## 1. Vision

A repo owner pastes an issue into their repo, assigns it to `@copilot`, and walks away. Copilot scaffolds the entire maintainer system. From that point on, the orchestrator runs weekly, using Copilot as its primary execution engine (free for open source), managing PRs, issues, and its own upgrades autonomously.

No Python. No Node. No understanding of GitHub Actions. One issue. Done.

---

## 2. The Setup Experience

### 2.1 What the User Does

1. Go to `github.com/your-org/caretaker` (our repo)
2. Copy the setup issue template (prominently displayed in README)
3. Create a new issue in *their* repo with that text
4. Assign it to `@copilot`
5. Optionally add `ANTHROPIC_API_KEY` to repo secrets (for premium features)

That's it.

### 2.2 The Setup Issue Template

```markdown
## Setup Caretaker

@copilot Please set up the caretaker system for this repository.

### Instructions

1. Read the setup guide at:
   https://github.com/your-org/caretaker/blob/main/SETUP_AGENT.md

2. Follow the instructions in that guide exactly. It will tell you:
   - What files to create and where
   - How to detect this repo's language, CI setup, and branch protection
   - How to generate a config file with sensible defaults for this repo
   - How to write the GitHub Actions workflow
   - How to configure copilot instructions and agent files for ongoing maintenance

3. After creating all files, open a single PR with the changes.
   Title: "chore: setup caretaker"
   
4. In the PR description, include:
   - A summary of what was configured and why
   - Any secrets the repo owner needs to add manually
   - A checklist of optional features the owner can enable

### Context

This repo uses the caretaker system for automated repo management.
See: https://github.com/your-org/caretaker
```

### 2.3 What Copilot Does (Guided by SETUP_AGENT.md)

Our `SETUP_AGENT.md` is a detailed, structured prompt that Copilot follows. It lives in *our* central repo and Copilot fetches it via the URL. This file is the actual brains of the setup — it tells Copilot step by step what to analyze and generate.

Key things SETUP_AGENT.md instructs Copilot to do:

1. **Analyze the repo**: Detect language(s), existing CI workflows, branch protection, existing bots (Dependabot, Renovate), test frameworks, linting setup
2. **Generate config**: `.github/maintainer/config.yml` with defaults tuned to what it found
3. **Generate workflow**: `.github/workflows/maintainer.yml` — thin, version-pinned
4. **Generate Copilot integration files**:
   - Append to `.github/copilot-instructions.md` (project-level Copilot memory)
   - Create `.github/agents/maintainer-pr.md` (PR agent persona)
   - Create `.github/agents/maintainer-issue.md` (Issue agent persona)
5. **Pin version**: `.github/maintainer/.version`
6. **Open one PR** with everything

### 2.4 Why This Works

- Copilot can read URLs — our `SETUP_AGENT.md` is the "brain" that Copilot downloads and follows
- Copilot can analyze a repo's structure, languages, and existing config
- Copilot can create files and open PRs
- The user doesn't install anything, run any CLI, or understand any tooling
- The `SETUP_AGENT.md` is versioned in our repo — we can improve the setup experience without touching any consumer repo

---

## 3. Copilot Integration Architecture

### 3.1 Integration Seams

Copilot has several native extension points we use as boundaries between our orchestrator and Copilot's execution:

```
┌─────────────────────────────────────────────────────┐
│  Consumer Repo                                      │
│                                                     │
│  .github/                                           │
│    copilot-instructions.md    ← project-level       │
│    │                            Copilot memory      │
│    │                            (we append to this) │
│    │                                                │
│    agents/                                          │
│      maintainer-pr.md         ← PR agent persona    │
│      maintainer-issue.md      ← Issue agent persona │
│      maintainer-upgrade.md    ← Upgrade agent       │
│    │                                                │
│    maintainer/                                      │
│      config.yml               ← repo settings       │
│      .version                 ← pinned version      │
│    │                                                │
│    workflows/                                       │
│      maintainer.yml           ← orchestrator entry  │
│                                                     │
└─────────────────────────────────────────────────────┘
```

### 3.2 copilot-instructions.md (Project Memory)

This is Copilot's persistent memory for the repo. Our setup appends a section:

```markdown
<!-- Added by caretaker -->
## Caretaker System

This repository uses the caretaker automated management system.

### How it works
- An orchestrator runs weekly via GitHub Actions
- It creates issues and assigns them to @copilot for execution
- When @copilot opens PRs, the orchestrator monitors them through CI, review, and merge
- The orchestrator communicates with @copilot via structured issue/PR comments

### When assigned an issue by caretaker
- Read the full issue body carefully — it contains structured instructions
- Follow the instructions exactly as written
- If unclear, comment on the issue asking for clarification
- Always ensure CI passes before considering work complete
- Reference the agent file for your role: .github/agents/maintainer-pr.md or maintainer-issue.md

### Conventions
- Branch naming: maintainer/{type}-{description}
- Commit messages: chore(maintainer): {description}
- Always run existing tests before pushing
- Do not modify .github/maintainer/ files unless explicitly instructed
```

### 3.3 Agent Files (Copilot Personas)

Agent files (`.github/agents/`) give Copilot specialized personas. Our orchestrator references these when dispatching work.

**`.github/agents/maintainer-pr.md`**:
```markdown
# PR Maintenance Agent

You are a PR maintenance agent for this repository. You are invoked by the 
caretaker orchestrator to fix issues on pull requests.

## Your capabilities
- Fix failing CI builds (test failures, lint errors, type errors, build errors)
- Address code review comments (rename variables, add tests, fix formatting)
- Rebase branches to resolve merge conflicts
- Apply small, targeted code changes

## Your constraints
- Only modify files directly related to the issue described in your assignment
- Never modify .github/maintainer/ configuration files
- Never force push
- If you can't resolve an issue after 2 attempts, comment explaining what you tried
  and what's blocking you
- Always ensure all existing tests still pass after your changes

## Communication protocol
- The orchestrator communicates via structured comments on the PR
- Each comment contains a TASK block with specific instructions
- Respond with a RESULT block when you've pushed your fix
- If blocked, respond with a BLOCKED block explaining why

## Example task from orchestrator:
```
TASK: Fix CI failure
TYPE: TEST_FAILURE  
JOB: test-unit
ERROR: FAIL src/parser.test.ts - Expected null, Received undefined
ACTION: Fix parseConfig to return null for empty input. Run all tests.
```
```

**`.github/agents/maintainer-issue.md`**:
```markdown
# Issue Execution Agent

You are an issue execution agent. The caretaker orchestrator assigns 
you issues that describe specific changes to make to this codebase.

## Your workflow
1. Read the issue body completely — it contains structured context
2. Understand the acceptance criteria in the CRITERIA block
3. Create a branch: maintainer/{issue-number}-{short-description}
4. Implement the changes described
5. Ensure all tests pass
6. Open a PR referencing the issue (Fixes #N)

## Your constraints
- Implement exactly what the issue describes — no scope creep
- If the issue is ambiguous, comment asking for clarification BEFORE starting work
- Keep PRs focused and small. If the issue is large, comment proposing a breakdown.
- Write tests for new functionality
- Follow existing code style and conventions in this repo

## Communication
- Comment on the issue with your implementation plan before starting
- Reference specific files and functions you plan to modify
- After opening the PR, comment on the issue linking to it
```

### 3.4 How the Orchestrator Uses These Seams

The orchestrator itself is a Python process running in GitHub Actions. It doesn't execute code changes — it *manages* Copilot, which does.

```
Orchestrator (Python, runs in GH Actions)
  │
  ├── Reads config.yml
  ├── Reads repo state (open PRs, issues, CI status)
  ├── Decides what needs to happen
  │
  ├── For code changes:
  │     Creates/updates GitHub Issues with structured task descriptions
  │     Assigns to @copilot
  │     Copilot reads agent file + issue → does the work
  │
  ├── For PR management:
  │     Posts structured comments on PRs
  │     @mentions copilot with specific fix instructions
  │     Copilot reads agent file + comment → pushes fix
  │
  └── For escalation:
        Labels issue/PR
        Tags repo owner
        Posts human-readable summary
```

---

## 4. LLM Strategy: Copilot-First

### 4.1 Model Allocation

| Task | Engine | Why |
|---|---|---|
| Code changes (bug fixes, new features) | Copilot (free for OSS) | Native GitHub integration, creates PRs directly |
| CI failure fix | Copilot | Can read code, write fixes, push commits |
| Review comment resolution | Copilot | Understands code context natively |
| Rebase / conflict resolution | Copilot | Git operations are native |
| Issue triage + classification | Orchestrator logic + Copilot | Pattern matching on labels, keywords; Copilot for ambiguous cases |
| PR state evaluation | Orchestrator logic | Deterministic — API calls to check CI, reviews, conflicts |
| Upgrade planning | Orchestrator logic | Read releases.json, compare versions, deterministic |
| Escalation summaries | Copilot (or Claude if available) | Natural language synthesis |
| Complex multi-file refactors | Copilot | Its strongest use case |
| Config migration (breaking upgrades) | Copilot via issue | Issue describes exact changes; Copilot executes |

### 4.2 Optional Claude Tier

If the user adds `ANTHROPIC_API_KEY` to their repo secrets, the orchestrator unlocks premium capabilities:

```yaml
# .github/maintainer/config.yml
llm:
  # Copilot is always available (free)
  # Claude is used when ANTHROPIC_API_KEY secret exists
  claude_features:
    - ci_log_analysis          # Better at parsing long unstructured logs
    - architectural_review     # Can evaluate design-level PR comments
    - upgrade_impact_analysis  # Reasons about breaking change implications
    - issue_decomposition      # Breaks large issues into implementable chunks
    - cross_repo_planning      # Multi-repo dependency awareness (future)
```

Without Claude, everything still works — the orchestrator uses simpler heuristics and leans harder on Copilot. With Claude, the orchestrator makes smarter decisions about *what* to ask Copilot to do.

### 4.3 The Orchestrator is a Manager, Not a Coder

Critical distinction: the orchestrator never writes code. It:
- **Observes**: reads GitHub state via API
- **Decides**: applies policy + optional LLM reasoning
- **Delegates**: creates issues / posts comments for Copilot
- **Monitors**: checks if Copilot's work succeeded
- **Escalates**: tags humans when automation isn't enough

This means the orchestrator is small. Most of its "intelligence" is in how it composes instructions for Copilot and how it evaluates results.

---

## 5. PR Agent (Updated)

### 5.1 State Machine

(Unchanged from original plan — see PR Agent Plan for full state machine)

### 5.2 Copilot Interaction Protocol

The PR Agent communicates with Copilot exclusively via PR comments using a structured format that Copilot's agent file teaches it to understand:

**Orchestrator → Copilot (PR comment):**
```markdown
@copilot

<!-- caretaker:task -->
TASK: Fix CI failure
TYPE: TEST_FAILURE
JOB: test-unit
ATTEMPT: 1 of 2
PRIORITY: high

**Error output:**
```
FAIL src/parser.test.ts
  ● parseConfig › should handle empty input
    Expected: null
    Received: undefined
```

**What to do:**
1. Fix the `parseConfig` function to return `null` instead of `undefined` for empty input
2. Verify all tests pass locally before pushing
3. Reply with a RESULT block when done

**Context:**
This PR was opened to fix #38. The original change introduced a regression in empty input handling.
<!-- /caretaker:task -->
```

**Copilot → Orchestrator (expected response, trained via agent file):**
```markdown
<!-- caretaker:result -->
RESULT: FIXED
CHANGES: Modified src/parser.ts line 42 — changed `return undefined` to `return null`
TESTS: All 47 tests passing
COMMIT: abc123f
<!-- /caretaker:result -->
```

The structured comment blocks (`<!-- caretaker:task -->`) serve dual purpose:
- Machine-parseable by the orchestrator on next run
- Human-readable for the repo owner reviewing the PR

### 5.3 Failure Recovery

If Copilot doesn't respond with a RESULT block (common — it may just push silently):
1. Orchestrator checks for new commits since its last comment
2. If new commits exist → re-evaluate CI status
3. If no new commits after timeout → re-prompt (attempt 2)
4. If still no response → escalate to repo owner

If Copilot responds with BLOCKED:
1. Orchestrator reads the blocker description
2. If it's something the orchestrator can resolve (e.g., "I need the test fixtures") → provide context
3. If it's architectural → escalate

---

## 6. Issue Agent

### 6.1 Responsibilities

The Issue Agent handles the intake side of the pipeline:

1. **Triage incoming issues**: External users open issues. The agent classifies them.
2. **Dispatch to Copilot**: For implementable issues, creates a structured assignment.
3. **Manage issue lifecycle**: Track issues from open → assigned → PR opened → merged → closed.
4. **Communicate with reporters**: Keep issue reporters informed about progress.
5. **Plan work**: For complex issues, decompose into smaller actionable issues.

### 6.2 Issue Classification

On each run, the Issue Agent scans open issues and classifies them:

| Classification | Action |
|---|---|
| `BUG_SIMPLE` | Assign to Copilot with fix instructions. Copilot opens PR. |
| `BUG_COMPLEX` | Decompose into sub-issues. Assign individually to Copilot. |
| `FEATURE_SMALL` | Assign to Copilot if acceptance criteria are clear. |
| `FEATURE_LARGE` | Comment asking for more detail or decomposition. Tag owner if needed. |
| `QUESTION` | Attempt to answer from repo context (README, docs, code). Close if answered. |
| `DUPLICATE` | Link to existing issue. Close with comment. |
| `STALE` | Comment asking if still relevant. Close after configurable timeout. |
| `INFRA_OR_CONFIG` | Escalate to owner — Copilot can't manage secrets, permissions, etc. |
| `MAINTAINER_INTERNAL` | Issues created by the orchestrator itself — skip triage. |

### 6.3 Dispatching to Copilot

When the Issue Agent decides an issue is ready for implementation, it transforms it into a structured assignment:

**Original issue (from external user):**
```markdown
## Bug: Parser crashes on empty JSON

When I pass `{}` to the parser, it throws a NullPointerException.

Steps to reproduce:
1. Call parseConfig("{}")
2. Observe crash
```

**Issue Agent creates a new issue (or updates the existing one):**
```markdown
## [Maintainer] Fix: Parser crashes on empty JSON object input

Fixes #52 (reported by @external-user)

@copilot Please implement this fix. See `.github/agents/maintainer-issue.md` for your workflow.

<!-- caretaker:assignment -->
TYPE: BUG_SIMPLE
SOURCE_ISSUE: #52
PRIORITY: medium

**Root cause analysis:**
`parseConfig` in `src/parser.ts` doesn't handle the case where the JSON object 
has no keys. The null check on line 38 tests for `null` input but not empty objects.

**Acceptance criteria:**
- [ ] `parseConfig("{}")` returns an empty config object (not null, not crash)
- [ ] Add test case for empty JSON object input
- [ ] Add test case for JSON object with only whitespace keys
- [ ] All existing tests continue to pass

**Files likely involved:**
- `src/parser.ts` (line ~38)
- `src/parser.test.ts`
<!-- /caretaker:assignment -->
```

### 6.4 Managing the Issue → PR → Merge Lifecycle

```
External user opens issue
  │
  ▼
Issue Agent triages → classifies as BUG_SIMPLE
  │
  ▼
Issue Agent creates structured assignment (or edits issue in place)
  │ assigns to @copilot
  ▼
Copilot reads agent file + issue → creates branch → opens PR
  │ PR references "Fixes #52"
  ▼
PR Agent picks up the new PR → enters state machine
  │ monitors CI, reviews, manages Copilot interaction
  ▼
PR merged → GitHub auto-closes #52
  │
  ▼
Issue Agent detects closure → comments on #52:
  "This was fixed in PR #53, merged in commit abc123. 
   Thanks for reporting @external-user!"
```

---

## 7. Orchestrator

### 7.1 Invocation Model

The orchestrator runs as a Python process in GitHub Actions. It's lightweight — its job is to read state, make decisions, and delegate.

```yaml
# Triggers
on:
  schedule:
    - cron: '0 8 * * 1'        # Weekly full run
  pull_request:                  # Event-driven PR monitoring
  issues:                        # Event-driven issue triage
  issue_comment:                 # Catch Copilot/owner responses
  workflow_dispatch:              # Manual trigger
```

### 7.2 Run Modes

```python
class RunMode(Enum):
    FULL = "full"              # Run all agents
    PR_ONLY = "pr-only"        # Just PR agent
    ISSUE_ONLY = "issue-only"  # Just Issue agent  
    UPGRADE_ONLY = "upgrade"   # Just check for upgrades
    DRY_RUN = "dry-run"        # Read-only, report what would happen
    EVENT = "event"            # React to a specific webhook event
```

### 7.3 Orchestration Flow (Full Run)

```python
async def run_full(self, repo: str, config: MaintainerConfig):
    # 1. Upgrade check first (always)
    upgrade_report = await self.upgrade_agent.run(repo, config)
    if upgrade_report.upgrade_available:
        # Upgrade agent handles its own PR creation
        # Don't block other work on upgrade
        pass
    
    # 2. Issue triage
    issue_report = await self.issue_agent.run(repo, config)
    # issue_report.dispatched = issues assigned to Copilot this run
    # issue_report.escalated = issues needing human attention
    
    # 3. PR management
    pr_report = await self.pr_agent.run(repo, config)
    # pr_report.merged = PRs successfully merged
    # pr_report.escalated = PRs needing human attention
    
    # 4. Cross-agent reconciliation
    # - Close issues whose PRs were merged
    # - Detect orphaned PRs (no linked issue)
    # - Detect stale assignments (Copilot never responded)
    await self.reconcile(issue_report, pr_report)
    
    # 5. Generate run summary
    summary = self.generate_summary(upgrade_report, issue_report, pr_report)
    await self.post_summary(repo, summary)
```

### 7.4 Run Summary

The orchestrator posts a summary to a tracking issue after each full run:

```markdown
## Maintainer Run — April 14, 2026

### PR Agent
- 3 PRs monitored
- 1 merged (#45 — Copilot-authored, CI green, auto-merged)
- 1 awaiting Copilot fix (#47 — CI failure, attempt 1 of 2 posted)
- 1 escalated (#48 — architectural review comment, tagged @owner)

### Issue Agent
- 5 issues triaged
- 2 assigned to Copilot (#50 BUG_SIMPLE, #51 FEATURE_SMALL)
- 1 answered and closed (#49 QUESTION)
- 1 marked duplicate of #32 (#52)
- 1 escalated (#53 FEATURE_LARGE — needs decomposition)

### Upgrade Agent
- Current version: 1.3.2
- Latest version: 1.4.0 (non-breaking)
- Upgrade PR: #54 (auto-merge eligible)

### Next run: April 21, 2026
```

---

## 8. Distribution & Self-Upgrade (Updated)

### 8.1 Consumer Repo Footprint

After Copilot runs the setup issue, the consumer repo has:

```
.github/
  copilot-instructions.md         ← appended (not overwritten)
  agents/
    maintainer-pr.md              ← PR agent persona for Copilot
    maintainer-issue.md           ← Issue agent persona for Copilot
    maintainer-upgrade.md         ← Upgrade agent persona for Copilot
  maintainer/
    config.yml                    ← repo-specific settings
    .version                      ← e.g. "1.3.2"
  workflows/
    maintainer.yml                ← thin workflow (~40 lines)
```

No Python files. No node_modules. No vendored code. Just config and Copilot instructions.

### 8.2 The Workflow File

```yaml
name: Caretaker

on:
  schedule:
    - cron: '0 8 * * 1'
  pull_request:
    types: [opened, synchronize, reopened]
  pull_request_review:
    types: [submitted]
  check_suite:
    types: [completed]
  issues:
    types: [opened, labeled]
  issue_comment:
    types: [created]
  workflow_dispatch:
    inputs:
      mode:
        description: 'Run mode'
        required: false
        default: 'full'
        type: choice
        options: [full, pr-only, issue-only, upgrade-only, dry-run]

permissions:
  contents: write
  issues: write
  pull-requests: write

jobs:
  maintain:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'
          cache: 'pip'

      - name: Install caretaker
        run: |
          VERSION=$(cat .github/maintainer/.version)
          pip install "caretaker==${VERSION}"

      - name: Run
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}  # optional
        run: |
          caretaker run \
            --config .github/maintainer/config.yml \
            --mode "${{ github.event.inputs.mode || 'full' }}" \
            --event-type "${{ github.event_name }}" \
            --event-payload '${{ toJSON(github.event) }}'
```

### 8.3 Upgrade Flow (Copilot-Powered)

For non-breaking upgrades, the Upgrade Agent can just create an issue:

```markdown
## [Maintainer] Upgrade caretaker to 1.4.0

@copilot Please upgrade the caretaker version.

<!-- caretaker:assignment -->
TYPE: UPGRADE
CURRENT_VERSION: 1.3.2
TARGET_VERSION: 1.4.0
BREAKING: false

**Changes required:**
1. Update `.github/maintainer/.version` to contain: `1.4.0`
2. No config changes needed for this version

**Release notes:**
https://github.com/your-org/caretaker/releases/tag/v1.4.0

**Changelog highlights:**
- Improved review comment classification
- Better flaky test detection
- No config schema changes
<!-- /caretaker:assignment -->
```

For breaking upgrades, the issue includes migration instructions that Copilot follows:

```markdown
<!-- caretaker:assignment -->
TYPE: UPGRADE
CURRENT_VERSION: 1.4.0
TARGET_VERSION: 2.0.0
BREAKING: true
REQUIRES_HUMAN_REVIEW: true

**Changes required:**
1. Update `.github/maintainer/.version` to contain: `2.0.0`
2. Update `.github/maintainer/config.yml`:
   - Move `pr_agent.nitpick_threshold` to `pr_agent.review.nitpick_threshold`
   - Add new required field: `orchestrator.summary_issue: true`
3. Update `.github/agents/maintainer-pr.md` — fetch latest from:
   https://raw.githubusercontent.com/your-org/caretaker/v2.0.0/dist/templates/agents/maintainer-pr.md
4. Update `.github/agents/maintainer-issue.md` — fetch latest from above
5. Append to `.github/copilot-instructions.md` — add v2 section from:
   https://raw.githubusercontent.com/your-org/caretaker/v2.0.0/dist/templates/copilot-instructions-append.md

**Do NOT auto-merge this PR. Label it `maintainer:breaking` so the owner reviews.**
<!-- /caretaker:assignment -->
```

### 8.4 Agent File Updates

The agent files (`.github/agents/*.md`) are themselves versioned. When we release a new version that changes the communication protocol or adds capabilities, the upgrade issue instructs Copilot to fetch the latest templates from our repo and replace the local copies. This is how the "agents upgrade themselves" — the orchestrator tells Copilot to update the instructions that Copilot itself reads.

### 8.5 Bootstrap Contract

These artifacts are the stable interface between versions and must never have breaking schema changes:

1. **`releases.json`** — append-only release manifest
2. **`SETUP_AGENT.md`** — setup prompt (can improve but must remain backwards-compatible)
3. **Issue comment format** — `<!-- caretaker:task -->` blocks
4. **`.version` file** — always a plain semver string

Everything else (agent files, config schema, copilot-instructions content) is versionable and upgradeable through the system itself.

---

## 9. Configuration

### 9.1 Full Config Schema

```yaml
# .github/maintainer/config.yml
version: v1  # config schema version

orchestrator:
  schedule: weekly              # weekly | daily | manual
  summary_issue: true           # Post run summaries to a tracking issue
  dry_run: false                # Read-only mode (no writes)

pr_agent:
  enabled: true
  auto_merge:
    copilot_prs: true           # Auto-merge PRs created by Copilot via maintainer
    dependabot_prs: true        # Auto-merge dependency updates
    human_prs: false            # Never auto-merge human PRs
    merge_method: squash        # squash | merge | rebase
  copilot:
    max_retries: 2              # Re-prompt attempts before escalation
    retry_window_hours: 24      # Time to wait between retries
    context_injection: true     # Include originating issue context in fix requests
  ci:
    flaky_retries: 1            # Re-trigger CI before blaming code
    ignore_jobs: []             # Jobs that aren't merge-blocking
  review:
    auto_approve_copilot: false # Agent approves Copilot PRs (needs review permission)
    nitpick_threshold: low      # low = address all, high = skip nitpicks

issue_agent:
  enabled: true
  auto_assign_bugs: true        # Auto-assign BUG_SIMPLE to Copilot
  auto_assign_features: false   # Require human approval for features
  auto_close_stale_days: 30     # Close stale issues after N days
  auto_close_questions: true    # Close issues answered by the agent
  labels:
    bug: ["bug"]
    feature: ["enhancement", "feature"]
    question: ["question"]

upgrade_agent:
  enabled: true
  strategy: auto-minor          # auto-minor | auto-patch | latest | pinned | manual
  channel: stable               # stable | preview
  auto_merge_non_breaking: true

escalation:
  targets: []                   # GitHub usernames. Empty = repo owner.
  stale_days: 7                 # Days before stale escalation gets attention
  labels: ["maintainer:escalated"]

llm:
  # Copilot is always the execution engine (free)
  # Claude is optional for enhanced decision-making
  claude_enabled: auto          # auto (detect secret) | true | false
  claude_features:              # Only used if claude_enabled
    - ci_log_analysis
    - architectural_review
    - issue_decomposition
    - upgrade_impact_analysis
```

---

## 10. Central Repo Structure

```
caretaker/
  src/
    caretaker/
      __init__.py
      cli.py                    # CLI entrypoint
      orchestrator.py           # Main orchestration loop
      pr_agent/
        __init__.py
        agent.py                # PR Agent logic
        states.py               # State machine
        copilot.py              # Copilot comment protocol
        ci_triage.py            # CI failure analysis
        review.py               # Review comment handling
        merge.py                # Merge policy evaluation
      issue_agent/
        __init__.py
        agent.py                # Issue Agent logic
        classifier.py           # Issue classification
        dispatcher.py           # Copilot assignment creation
      upgrade_agent/
        __init__.py
        agent.py                # Upgrade Agent logic
        release_checker.py      # Fetch and parse releases.json
        planner.py              # Upgrade path planning
      github_client/
        __init__.py
        api.py                  # GitHub REST/GraphQL abstraction
        models.py               # PR, Issue, Comment data models
      llm/
        __init__.py
        copilot.py              # Copilot interaction (via GitHub comments)
        claude.py               # Claude API adapter (optional)
        router.py               # Route tasks to appropriate model
      state/
        __init__.py
        tracker.py              # State persistence (issue-backed)
        models.py               # State data models
  
  dist/
    templates/
      workflows/
        maintainer.yml          # Consumer workflow template
      agents/
        maintainer-pr.md        # PR agent persona template
        maintainer-issue.md     # Issue agent persona template
        maintainer-upgrade.md   # Upgrade agent persona template
      copilot-instructions-append.md  # Section to append to copilot-instructions
      config-default.yml        # Default config template
    SETUP_AGENT.md              # The setup prompt Copilot follows

  schema/
    config.v1.schema.json       # Config validation schema

  releases.json                 # Machine-readable release manifest
  CHANGELOG.md                  # Structured changelog
  UPGRADE_GUIDE.md              # Breaking change migration guides
  
  tests/
    test_pr_agent/
    test_issue_agent/
    test_upgrade_agent/
    test_upgrade_paths/         # Version upgrade simulation tests
  
  pyproject.toml
  README.md
```

---

## 11. Implementation Phases

### Phase 0 — Skeleton + Setup + Upgrade
**Goal: A user can paste an issue, get everything scaffolded, and receive future upgrades.**

1. Central repo structure with `pyproject.toml`, CLI entrypoint, PyPI publishing
2. `SETUP_AGENT.md` — the setup prompt
3. `dist/templates/` — all consumer scaffolding templates
4. Upgrade Agent — version check, release manifest, creates upgrade issues
5. `releases.json` — initial release manifest
6. Consumer workflow template
7. Agent file templates (PR, Issue, Upgrade personas)
8. Dogfood: central repo uses itself

**Deliverable**: User can set up any repo and receive automatic non-breaking upgrades.

### Phase 1 — PR Agent (Read-Only → Interactive)
**Goal: The orchestrator monitors PRs and interacts with Copilot to fix issues.**

1. PR discovery + classification
2. CI status monitoring
3. CI failure triage (regex patterns → optional Claude enhancement)
4. Structured comment protocol for Copilot interaction
5. Retry loop + escalation
6. Auto-merge for policy-eligible PRs
7. Run summary reporting

**Deliverable**: Copilot-authored PRs are shepherded from creation to merge without human intervention.

### Phase 2 — Issue Agent
**Goal: External issues are triaged and dispatched to Copilot automatically.**

1. Issue classification (label-based → optional LLM-enhanced)
2. Structured assignment creation
3. Issue → PR lifecycle tracking
4. Stale issue management
5. Duplicate detection
6. Reporter communication

**Deliverable**: Bug reports become PRs become merged fixes with minimal human involvement.

### Phase 3 — Orchestrator Intelligence + Multi-Repo
**Goal: Smarter cross-agent coordination and portfolio-level management.**

1. Cross-agent reconciliation (orphaned PRs, stale assignments)
2. Velocity metrics (time-to-merge, escalation rate, Copilot success rate)
3. Multi-repo awareness (optional, for users with many repos)
4. Claude-enhanced features (CI log analysis, architectural review)
5. Custom hooks / plugin interface for repo-specific logic

**Deliverable**: Production-grade autonomous repo management at scale.

---

## 12. Key Design Decisions Summary

| Decision | Choice | Rationale |
|---|---|---|
| Setup mechanism | Issue assigned to Copilot | Zero tooling knowledge required |
| Primary execution engine | Copilot (free for OSS) | No API costs for basic operation |
| Optional intelligence layer | Claude (if secret present) | Better reasoning for complex decisions |
| Orchestrator language | Python | Best libs for GitHub API, LLM integration |
| Consumer repo footprint | Config + agent files only | No vendored code, all logic in central package |
| Agent ↔ Copilot protocol | Structured PR/issue comments | Machine-parseable, human-readable, auditable |
| State persistence | Tracking issue (issue-backed) | No external deps, visible, auditable |
| Upgrade mechanism | Copilot executes upgrade issues | Same pattern as all other work |
| Version pinning | `.version` file + pip install | Atomic, diffable, Copilot-updatable |
| Config evolution | Versioned schemas + migration guide | Old orchestrator can read new release manifest |