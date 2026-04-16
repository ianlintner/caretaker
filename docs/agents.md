# Agents

Caretaker is organized as a set of focused agents coordinated by the orchestrator.

## Core agents

| Agent            | Responsibility                                                                |
| ---------------- | ----------------------------------------------------------------------------- |
| PR agent         | monitors pull requests, triages CI failures, requests fixes, merges when safe |
| Issue agent      | classifies issues and dispatches work to Copilot or escalates it              |
| DevOps agent     | turns default-branch CI failures into actionable fix issues                   |
| Self-heal agent  | investigates caretaker's own workflow failures                                |
| Security agent   | triages Dependabot, code scanning, and secret scanning alerts                 |
| Dependency agent | reviews Dependabot PRs, auto-merges safe bumps, posts digests                 |
| Docs agent       | reconciles merged PRs into changelog/docs updates                             |
| Charlie agent    | closes duplicate or abandoned caretaker-managed issues and PRs                |
| Stale agent      | warns/closes stale work and prunes merged branches                            |
| Upgrade agent    | detects new caretaker releases and opens upgrade work                         |
| Escalation agent | creates a digest for work requiring human attention                           |

## Detailed descriptions

### PR Agent

**Purpose:** Ensure all pull requests move toward merge or resolution.

**What it does:**

- Monitors all open PRs in real-time via GitHub webhooks
- Detects CI failures and categorizes them (test, lint, build, type errors)
- Posts structured comments to `@copilot` requesting fixes (via `COPILOT_PAT`)
- Implements retry logic with configurable max attempts
- Auto-merges Copilot, Dependabot, and optionally human PRs when CI passes
- Handles flaky tests with configurable retry counts
- Analyzes review state and can auto-approve Copilot PRs
- Escalates to humans after max retries exhausted

**Key config:**

```yaml
pr_agent:
  auto_merge:
    copilot_prs: true
    dependabot_prs: true
    human_prs: false
  copilot:
    max_retries: 2
    retry_window_hours: 24
  ci:
    flaky_retries: 1
```

### Issue Agent

**Purpose:** Triage and route incoming issues to the right destination.

**What it does:**

- Classifies new issues as bug, feature, question, or duplicate
- Auto-assigns simple bugs to Copilot when configured
- Tracks issue → PR → merge lifecycle in state
- Auto-closes answered questions after inactivity
- Detects and links duplicate issues
- Escalates complex or ambiguous issues to repo owners
- Maintains issue labels and metadata

**Key config:**

```yaml
issue_agent:
  auto_assign_bugs: true
  auto_assign_features: false
  auto_close_stale_days: 30
  auto_close_questions: true
```

### DevOps Agent

**Purpose:** Keep the default branch CI healthy.

**What it does:**

- Monitors default-branch (usually `main`) workflow runs
- Detects CI failures on the latest commit
- Creates detailed fix issues with error context
- Deduplicates similar failures using signatures
- Enforces cooldown periods to prevent issue spam
- Assigns fix issues to Copilot for resolution
- Limits max issues per run to avoid overwhelming the queue

**Key config:**

```yaml
devops_agent:
  target_branch: main
  max_issues_per_run: 3
  dedup_open_issues: true
  cooldown_hours: 6
```

### Self-Heal Agent

**Purpose:** Ensure caretaker itself stays operational.

**What it does:**

- Monitors caretaker's own workflow runs
- Detects failures in orchestrator execution
- Creates self-diagnosis issues with error logs
- Optionally reports bugs upstream to caretaker repo
- Implements cooldown to prevent duplicate reports
- Ensures the system can maintain itself

**Key config:**

```yaml
self_heal_agent:
  report_upstream: true
  is_upstream_repo: false # set true for caretaker repo itself
  cooldown_hours: 6
```

### Security Agent

**Purpose:** Triage and track security findings.

**What it does:**

- Monitors Dependabot vulnerability alerts
- Tracks code scanning findings (CodeQL, etc.)
- Watches secret scanning alerts
- Filters by minimum severity threshold
- Creates remediation issues with full context
- Supports false positive suppression rules
- Limits max issues per run to avoid alert fatigue

**Key config:**

```yaml
security_agent:
  min_severity: medium
  max_issues_per_run: 5
  include_dependabot: true
  include_code_scanning: true
  include_secret_scanning: true
```

### Dependency Agent

**Purpose:** Keep dependencies up to date safely.

**What it does:**

- Reviews all Dependabot PRs
- Auto-merges patch updates when tests pass
- Auto-merges minor updates when configured
- Posts weekly digest of dependency changes
- Uses smart merge strategies (squash/merge/rebase)
- Coordinates with PR agent for CI checks
- Escalates major version updates to humans

**Key config:**

```yaml
dependency_agent:
  auto_merge_patch: true
  auto_merge_minor: true
  post_digest: true
  merge_method: squash
```

### Docs Agent

**Purpose:** Keep documentation synchronized with code changes.

**What it does:**

- Scans recently merged PRs (configurable lookback)
- Generates changelog entries from PR metadata
- Updates `CHANGELOG.md` with categorized changes
- Optionally updates README or other docs
- Creates weekly docs update branches
- Posts summary of documentation changes
- Handles merge conflicts gracefully

**Key config:**

```yaml
docs_agent:
  lookback_days: 7
  changelog_path: CHANGELOG.md
  update_readme: false
```

### Charlie Agent

**Purpose:** Clean up operational clutter from caretaker's own work.

**What it does:**

- Detects duplicate caretaker-managed issues
- Detects duplicate caretaker-managed PRs
- Closes abandoned work after 14 days (shorter than general stale)
- Prevents operational work from snowballing
- Respects exempt labels (pinned, escalated)
- Runs before the broader stale agent
- Focused only on caretaker-generated content

**Key config:**

```yaml
charlie_agent:
  stale_days: 14
  close_duplicate_issues: true
  close_duplicate_prs: true
  exempt_labels:
    - pinned
    - maintainer:escalated
```

### Stale Agent

**Purpose:** Maintain a healthy backlog by closing stale work.

**What it does:**

- Warns issues/PRs after 60 days of inactivity (configurable)
- Closes issues/PRs 14 days after warning
- Deletes merged branches automatically
- Respects exempt labels for critical work
- Separate thresholds for issues vs PRs
- Leaves explanatory comments before closing
- Preserves security and dependency work

**Key config:**

```yaml
stale_agent:
  stale_days: 60
  close_after: 14
  close_stale_prs: true
  delete_merged_branches: true
  exempt_labels:
    - pinned
    - security
```

### Upgrade Agent

**Purpose:** Keep caretaker itself up to date in consumer repos.

**What it does:**

- Checks GitHub releases for new caretaker versions
- Compares against pinned `.github/maintainer/.version`
- Creates upgrade issues for Copilot to execute
- Supports multiple strategies: auto-minor, auto-patch, latest, pinned
- Handles breaking vs. non-breaking upgrades differently
- Supports preview channel for early adopters
- Auto-merges non-breaking upgrades when configured

**Key config:**

```yaml
upgrade_agent:
  strategy: auto-minor # auto-minor | auto-patch | latest | pinned
  channel: stable # stable | preview
  auto_merge_non_breaking: true
```

### Escalation Agent

**Purpose:** Aggregate work that needs human attention.

**What it does:**

- Creates or updates a human escalation digest issue
- Aggregates all escalated PRs and issues
- Groups by type: security, stale, complex bugs, etc.
- Notifies configured assignees
- Tracks escalation age for priority
- Updates digest on each run
- Provides clear action items for maintainers

**Key config:**

```yaml
human_escalation:
  post_digest_issue: true
  notify_assignees: []
escalation:
  stale_days: 7
  labels: ["maintainer:escalated"]
```

## How they collaborate

- the **orchestrator** decides which agent to run based on the event or scheduled mode
- the **GitHub client** is the shared integration layer for repo state and mutations
- the **state tracker** persists issue/PR tracking data in GitHub comments
- the **LLM layer** adds higher-quality reasoning where configured (`ANTHROPIC_API_KEY`)
- the **goal engine** (experimental) prioritizes agents based on quantitative goals
- the **memory store** provides persistent deduplication across runs

## Event mapping

| GitHub signal                                                     | Typical agent path                                 |
| ----------------------------------------------------------------- | -------------------------------------------------- |
| `pull_request`, `pull_request_review`, `check_run`, `check_suite` | PR agent                                           |
| `issues`, `issue_comment`                                         | Issue agent                                        |
| `workflow_run`                                                    | DevOps agent + Self-heal agent                     |
| `repository_vulnerability_alert`                                  | Security agent                                     |
| scheduled/manual full run                                         | orchestrator invokes the broader maintenance cycle |

## Copilot-facing instructions

The repo ships instruction files for Copilot-driven execution:

- `.github/copilot-instructions.md` — global project memory
- `.github/agents/maintainer-pr.md` — PR fix agent persona
- `.github/agents/maintainer-issue.md` — issue resolution agent persona
- `.github/agents/maintainer-upgrade.md` — upgrade agent persona
- `.github/agents/devops-build-triage.md` — CI fix agent persona
- `.github/agents/docs-update.md` — docs update agent persona
- `.github/agents/maintainer-self-heal.md` — self-heal agent persona
- `.github/agents/dependency-upgrade.md` — dependency agent persona
- `.github/agents/security-triage.md` — security agent persona
- `.github/agents/escalation-review.md` — escalation review agent persona

Those files define how Copilot should behave when Caretaker assigns work or requests changes.
