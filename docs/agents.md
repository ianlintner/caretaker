# Agents

Caretaker is organized as eighteen focused agents coordinated by a webhook
event pipeline and a scheduled reconciliation tick. The
[Architecture](architecture.md) page has the diagrams; this page covers
each agent in depth.

Agents are grouped by trigger:

| Tier | Triggered by | Agents |
| --- | --- | --- |
| **Event-driven** | GitHub webhooks | PR, PR Reviewer, PR CI Approver, Issue, Dependency, Security, DevOps |
| **Scheduled** | Reconciliation tick (Redis lease, single-pod) | Stale, Charlie, Docs, Upgrade, Escalation, Self-Heal |
| **Dispatch-time / advisory** | Invoked by other agents during dispatch | Review, Principal, Refactor, Perf, Migration, Test, Bootstrap |

## Event-driven tier

These agents react to GitHub events delivered via the App webhook
endpoint. The webhook receiver dedup'es and rate-limits, then enqueues
to a Redis Stream consumer group that fans out to the right agent.

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

The PR agent runs ten sub-flows; see
[pr-flows-diagrams.md](https://github.com/ianlintner/caretaker/blob/main/docs/pr-flows-diagrams.md)
for the full state machine.

### PR Reviewer

**Purpose:** Inline LLM code review.

Posts review comments on PRs using either an in-process LLM call or a
hand-off to `claude-code-action` / `opencode_local`. Tier-based model
selection picks Sonnet/Opus/Haiku based on PR size and risk. Runs as
a separate agent from the PR agent so review decisions stay
independent from merge decisions.

### PR CI Approver

**Purpose:** Surface and auto-approve stuck bot CI runs.

Some workflows require manual approval for first-time contributors or
forked PRs. This agent watches for stuck `action_required` runs from
trusted bots (Copilot, Dependabot) and approves them so CI can complete
without a human.

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

### DevOps Agent

**Purpose:** Keep the default branch CI healthy.

**What it does:**

- Monitors default-branch (usually `main`) workflow runs
- Detects CI failures on the latest commit
- Creates detailed fix issues with error context
- Deduplicates similar failures using signatures
- Enforces cooldown periods to prevent issue spam
- Routes fixes through the configured coding backend
- Limits max issues per run to avoid overwhelming the queue

**Key config:**

```yaml
devops_agent:
  target_branch: main
  max_issues_per_run: 3
  dedup_open_issues: true
  cooldown_hours: 6
```

## Scheduled tier

Triggered by the in-cluster `ReconciliationScheduler`, which holds a
Redis-backed lease so only one of the two `mcp_backend` replicas fans
out work each tick. The scheduler emits a synthetic schedule event per
installed repo so the same agent code paths run as in the webhook tier.

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

### Upgrade Agent

**Purpose:** Keep caretaker itself up to date in consumer repos.

**What it does:**

- Checks GitHub releases for new caretaker versions
- Compares against pinned `.github/maintainer/.version`
- Creates upgrade issues for the configured backend to execute
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

### Self-Heal Agent

**Purpose:** Ensure caretaker itself stays operational.

**What it does:**

- Monitors caretaker's own backend for runtime failures
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

## Dispatch-time / advisory tier

These agents are not directly triggered by webhooks or the scheduler.
They are invoked by other agents during dispatch — typically to grade,
review, or supplement work — and run inside the same process or are
hand-off targets through the `ExecutorDispatcher`.

### Review Agent

Grades runs, PRs, and issues against a rubric. Used by the evolution
loop to score shadow decisions.

### Principal Agent

Provides architectural review on larger changes. Targeted at PRs that
the PR agent flags as cross-cutting; routes through a higher-tier model
(Opus by default) for deeper analysis.

### Refactor Agent

Long-form refactor planning and execution. Typically routed through the
durable [coding-job pipeline](architecture.md#3-coding-job-lifecycle)
because refactors take longer than a single FastAPI request.

### Perf Agent

Performance-regression triage. Reads benchmark output from CI, identifies
hot spots, and creates fix issues with profiling artefacts attached.

### Migration Agent

Upgrade impact analysis. When the upgrade agent surfaces a breaking
release, the migration agent expands the impact scope, drafts a plan,
and (optionally) opens a tracking issue with stage gates.

### Test Agent

Test-coverage and test-failure heuristics. Used during CI-fix flows to
distinguish flaky from genuinely broken tests.

### Bootstrap Agent

Scaffolds caretaker setup files in new repos. The
[setup guide](https://github.com/ianlintner/caretaker/blob/main/setup-templates/SETUP_AGENT.md)
is the human-readable version; the bootstrap agent is the
machine-readable equivalent for automated rollouts.

## How they collaborate

- the **webhook receiver** in `mcp_backend` dedup's, rate-limits, and
  fans events out to a Redis Stream consumer group
- the **agent router** maps event types to event-driven agents via
  `EVENT_AGENT_MAP`
- the **`ExecutorDispatcher`** picks the coding backend per dispatch:
  label override → per-feature config provider → Copilot fallback
- the **`ReconciliationScheduler`** drives the scheduled tier on a tick,
  fanning out one synthetic event per installed repo
- the **state tracker** persists tracking state in GitHub itself
  (comments, labels), with derived state in MongoDB / Cosmos and Neo4j
- the **goal engine** (experimental) reorders agent execution by
  goal-impact when enabled

## Event mapping

| GitHub signal | Routed to |
| --- | --- |
| `pull_request`, `pull_request_review` | PR agent, PR reviewer |
| `check_run`, `check_suite`, `workflow_run` | DevOps agent, PR CI approver |
| `issues`, `issue_comment` | Issue agent |
| `repository_vulnerability_alert`, `code_scanning_alert`, `secret_scanning_alert` | Security agent |
| Dependabot PR | Dependency agent |
| scheduled tick (synthetic) | Stale, Charlie, Docs, Upgrade, Escalation, Self-Heal |

## Coding backends

When an agent needs to make a code change, it routes through the
`ExecutorDispatcher`, which selects one of four backends:

- **Copilot** — `@copilot` hand-off comment, the legacy default
- **Foundry** — in-process LLM tool loop, drives Azure AI Foundry or
  any LiteLLM-compatible provider
- **HandoffAgent** — tags PR/issue and lets `claude-code-action` or
  `opencode_local` GitHub Actions complete the work asynchronously
- **K8s Job** — durable per-task pod for longer-running work,
  brokered through Azure Service Bus and the
  `caretaker-job-dispatcher` deployment

Three labels override backend selection per item:

- `agent:custom` — force the custom executor (Foundry by default)
- `agent:copilot` — force the legacy path
- `agent:quarantine` — refuse dispatch entirely (for hostile or
  confusing items)

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

Those files define how Copilot should behave when Caretaker assigns
work or requests changes.
