# ReviewAgent design plan

## Objective

Design a backward-looking `ReviewAgent` that evaluates completed or mostly completed caretaker work across issues, pull requests, and prior caretaker runs. The agent should generate:

- structured scores
- concise notes
- a retrospective
- artifact outputs in both markdown and machine-readable formats
- optional summary comments on reviewed GitHub issues and pull requests

This plan is based on the current agent contract, registry wiring, orchestrator flow, tracker state, and disk-backed memory facilities.

## Current architecture observations

### Agent contract and registry fit

The shared agent contract is defined by `BaseAgent`, `AgentContext`, and `AgentResult` in `src/caretaker/agent_protocol.py`. Any new review-oriented agent should follow the same pattern:

- `name` identifies the agent for registry and logging
- `enabled` gates execution from config
- `execute` receives `OrchestratorState` and optional event payload
- `apply_summary` maps review metrics into `RunSummary`

The registry in `src/caretaker/registry.py` already supports mode-based registration, ordered execution, and per-agent summary application. This makes a new review-focused adapter straightforward to integrate.

### Orchestrator and persistence fit

The orchestrator in `src/caretaker/orchestrator.py` already provides the two persistence surfaces the review agent needs:

1. `StateTracker` persistence via `OrchestratorState.last_run` and `OrchestratorState.run_history`
2. optional `MemoryStore` persistence via `AgentContext.memory`

The orchestrator also already writes JSON reports through the existing `report_path` behavior and writes a memory snapshot artifact when enabled. The review design should reuse the same artifact-oriented operating model instead of inventing a separate persistence system.

### Existing history sources

The best historical inputs already present in the codebase are:

- `OrchestratorState.run_history` for bounded recent run summaries
- `OrchestratorState.tracked_prs` and `OrchestratorState.tracked_issues` for current lifecycle state
- `OrchestratorState.goal_history` for trend-like score history
- `MemoryStore` for richer, namespaced longitudinal memory beyond the tracker issue cap

`StateTracker` currently retains only the last 20 runs. That is sufficient for short-term retrospectives, but not enough for longer-term grading. The `ReviewAgent` should therefore treat tracker history as the short-window source of truth and `MemoryStore` as the long-window trend store.

## Recommended naming and scope

Use `ReviewAgent` as the primary name, with grading as one responsibility inside it.

Reasons:

- the user-visible output is broader than a grade alone
- the artifact includes notes, trends, scorecard, and retro analysis
- the architecture can later expose grading as a submodule or report section without renaming the agent

Suggested internal naming:

- runtime adapter: `ReviewAgentAdapter`
- domain service: `ReviewAgent`
- report models: `ReviewScorecard`, `ReviewRetro`, `ReviewArtifactManifest`

## 1. Agent structure, inputs, and outputs

### Placement

Recommended new files:

- `src/caretaker/review_agent/__init__.py`
- `src/caretaker/review_agent/agent.py`
- `src/caretaker/review_agent/models.py`
- `src/caretaker/review_agent/history.py`
- `src/caretaker/review_agent/reporting.py`

Recommended integration points:

- add `ReviewAgentAdapter` in `src/caretaker/agents.py`
- register a new mode such as `review`
- optionally include the agent in `full` after operational agents finish so it can review the completed run context

### Execution modes

Support two modes:

1. **scheduled retrospective mode**
   - reviews the latest completed caretaker run and recent historical trend window
   - ideal for nightly or weekly scoring

2. **targeted object review mode**
   - reviews a single issue, a single PR, or a specific caretaker run after completion
   - triggered by explicit CLI input or future event/manual dispatch

### Inputs

The agent should combine repository, state, and memory inputs.

#### Primary inputs

- `OrchestratorState`
- `RunSummary` context from the current or most recent run
- `MemoryStore` data from `AgentContext.memory`
- GitHub issue or PR metadata and comments
- optional event/dispatch payload identifying a target item

#### Recommended explicit review request envelope

Define an internal request model like:

```python
class ReviewRequest(BaseModel):
    target_kind: Literal["run", "issue", "pull_request", "batch"]
    target_number: int | None = None
    target_run_at: datetime | None = None
    lookback_runs: int = 10
    lookback_days: int = 30
    include_comments: bool = True
    include_memory: bool = True
    publish_summary_comment: bool = False
```

This keeps review logic deterministic and makes future CLI wiring easier.

### Review pipeline

Recommended pipeline inside the agent:

1. **Resolve target**
   - determine whether the review is for a PR, issue, run, or a batch window
2. **Collect evidence**
   - fetch current object details from GitHub
   - pull tracker state slices
   - load memory-backed historical facts
3. **Normalize evidence**
   - convert runs, issue state, PR state, retries, escalations, comments, and failures into a shared evidence model
4. **Score**
   - compute category scores and overall grade
5. **Generate notes**
   - produce short findings, strengths, weaknesses, anomalies, and recurring patterns
6. **Generate retrospective**
   - what went well
   - what failed
   - what to improve
   - what to stop or reduce
7. **Persist outputs**
   - logs
   - markdown artifact
   - JSON artifact
   - optional issue or PR comment
   - memory updates for future trend detection

### Outputs

The agent should produce both operational and artifact outputs.

#### `AgentResult`

Proposed `AgentResult.extra` fields:

- `reviews_completed`
- `artifacts_written`
- `summary_comment_targets`
- `average_score`
- `critical_findings`
- `trend_flags`

#### Logging output

Log one-line summaries for quick workflow inspection, for example:

- target reviewed
- final score and grade
- top positive and negative findings
- artifact paths written
- whether a summary comment was posted

#### Artifact output

Write at least:

- markdown human report
- JSON structured report
- optional manifest JSON pointing to all generated files

## 2. Integration with historical data and memory

### Design principle

The review agent should use a two-tier history model:

- **short-term truth from tracker state** for recent runs and current item lifecycle
- **long-term trend memory from `MemoryStore`** for recurring patterns, score drift, and repeated failure motifs

### History sources by concern

#### Run-level history

Source from:

- `OrchestratorState.last_run`
- `OrchestratorState.run_history`

Use for:

- recent error frequency
- merge and escalation trendlines
- recent average run health
- short retrospective context

#### PR and issue lifecycle history

Source from:

- `OrchestratorState.tracked_prs`
- `OrchestratorState.tracked_issues`

Use for:

- retry counts
- repeated escalation
- prolonged in-progress or fix-requested states
- orphaned work patterns

#### Long-window memory

Persist normalized review snapshots into `MemoryStore` under a dedicated namespace such as `review-agent`.

Recommended key strategy:

- `review-agent:run:<iso-timestamp>`
- `review-agent:pr:<number>:<iso-timestamp>`
- `review-agent:issue:<number>:<iso-timestamp>`
- `review-agent:trend:<period>`

Recommended stored JSON payload per review snapshot:

```json
{
  "target_kind": "pull_request",
  "target_number": 123,
  "reviewed_at": "2026-04-16T12:00:00Z",
  "overall_score": 78,
  "grade": "B",
  "dimension_scores": {
    "outcome": 85,
    "execution": 70,
    "reliability": 72,
    "maintainability": 80,
    "communication": 82
  },
  "flags": ["repeat_ci_failure", "slow_recovery"],
  "summary": "Merged after multiple CI retries and review churn"
}
```

### Trend detection strategy

The agent should not rely on free-form LLM summarization alone. It should derive explicit trend signals first, then optionally have the LLM explain them.

Recommended deterministic trend detectors:

1. **repeat failure signature detector**
   - repeated `summary.errors`
   - repeated CI failure categories
   - repeated escalation causes

2. **score drift detector**
   - compare latest score to moving average over last N reviews
   - flag sustained decline or improvement

3. **rework detector**
   - high PR `ci_attempts`
   - high `copilot_attempts`
   - repeated `FIX_REQUESTED` or `REVIEW_CHANGES_REQUESTED`

4. **throughput quality detector**
   - many completed items but poor grade
   - low merge quality masked by high volume

5. **communication gap detector**
   - sparse notes
   - unresolved review comments
   - escalation without sufficient explanation

### Memory responsibilities

The `ReviewAgent` should both **read** and **write** memory.

#### Reads

- prior review snapshots
- rolling aggregates
- previously flagged recurring issues

#### Writes

- the latest normalized review snapshot
- aggregated trend summaries
- recurring issue counters
- optional suppression markers to avoid duplicate summary comments

### Recommended fallback behavior

If `MemoryStore` is disabled or unavailable:

- continue using `run_history` and tracker state only
- emit a report note that long-window trend confidence is reduced
- still produce scorecards and artifacts

## 3. Retro scorecard schema and format

### Schema goals

The scorecard should be:

- easy for humans to scan in markdown
- stable for downstream tooling in JSON
- comparable over time
- broad enough to cover runs, issues, and PRs using a shared core model

### Recommended top-level schema

```json
{
  "schema_version": "v1",
  "agent": "review",
  "reviewed_at": "2026-04-16T12:00:00Z",
  "target": {
    "kind": "pull_request",
    "number": 123,
    "title": "Fix flaky CI job",
    "url": "https://github.com/owner/repo/pull/123"
  },
  "window": {
    "lookback_runs": 10,
    "lookback_days": 30
  },
  "overall": {
    "score": 78,
    "grade": "B",
    "confidence": 0.81,
    "status": "mixed"
  },
  "dimensions": {
    "outcome": {
      "score": 85,
      "weight": 0.30,
      "notes": ["Target outcome achieved"]
    },
    "execution": {
      "score": 70,
      "weight": 0.20,
      "notes": ["Required multiple retries"]
    },
    "reliability": {
      "score": 72,
      "weight": 0.20,
      "notes": ["CI instability affected confidence"]
    },
    "maintainability": {
      "score": 80,
      "weight": 0.15,
      "notes": ["State handling remained coherent"]
    },
    "communication": {
      "score": 82,
      "weight": 0.15,
      "notes": ["Comments and escalation context were adequate"]
    }
  },
  "findings": {
    "strengths": ["Issue and PR linkage stayed intact"],
    "weaknesses": ["Review churn increased cycle cost"],
    "recurring_issues": ["CI failure signature repeated across runs"],
    "anomalies": []
  },
  "retro": {
    "went_well": ["Fast detection of the failing state"],
    "failed": ["Fix validation required multiple loops"],
    "do_better": ["Classify flaky failures earlier"],
    "stop_or_less": ["Stop reposting similar fix prompts without new evidence"]
  },
  "evidence": {
    "run_summaries_considered": 10,
    "memory_entries_considered": 24,
    "tracker_signals": ["copilot_attempts", "prs_escalated"],
    "github_comments_considered": 6
  },
  "outputs": {
    "markdown_report_path": "artifacts/review/pr-123.md",
    "json_report_path": "artifacts/review/pr-123.json",
    "summary_comment_posted": true
  }
}
```

### Score dimensions

Use a shared weighted model with dimension-specific heuristics.

#### Core dimensions

- **Outcome**
  - was the item resolved successfully
  - did it converge to the intended end state
- **Execution quality**
  - number of retries
  - unnecessary churn
  - wasted loops
- **Reliability**
  - CI stability
  - repeated failures
  - escalation necessity
- **Maintainability**
  - cleanliness of state transitions
  - reduced manual cleanup burden
  - clear artifact trail
- **Communication**
  - quality of comments
  - clarity of escalation notes
  - usefulness for future operators

### Grade mapping

Suggested initial grade bands:

- 95 to 100: A+
- 90 to 94: A
- 85 to 89: A-
- 80 to 84: B+
- 75 to 79: B
- 70 to 74: B-
- 65 to 69: C+
- 60 to 64: C
- 50 to 59: D
- below 50: F

### Score heuristics by target type

#### Pull request review emphasis

Weight more heavily:

- CI behavior
- review churn
- merge outcome
- fix loop count

#### Issue review emphasis

Weight more heavily:

- classification quality
- routing quality
- whether issue reached PR or closure
- escalation appropriateness

#### Run review emphasis

Weight more heavily:

- summary health metrics
- error count
- escalation rate
- recurring failure signatures
- downstream artifact usefulness

### Markdown rendering format

Every markdown report should include:

1. header with target and grade
2. short executive summary
3. score table
4. key findings
5. trend section
6. retrospective section
7. evidence appendix
8. artifact manifest or pointers

Example markdown skeleton:

```md
# Review Report: PR #123

- Overall score: 78 `B`
- Confidence: 0.81
- Reviewed at: 2026-04-16T12:00:00Z

## Executive summary

Merged successfully, but quality was reduced by repeated CI churn and multiple fix cycles.

## Scorecard

| Dimension | Score | Notes |
| --- | ---: | --- |
| Outcome | 85 | Reached merge state |
| Execution | 70 | Multiple retries |
| Reliability | 72 | CI instability |
| Maintainability | 80 | State remained coherent |
| Communication | 82 | Review trail was useful |

## Retrospective

### What went well
- ...

### What failed
- ...

### What to do better
- ...

### What to stop or do less of
- ...
```

## 4. Artifact saving strategy

### Artifact goals

Artifacts should be:

- easy to upload from CI
- stable for maintainers and Copilot to read
- linked from logs and comments
- preserved independently from transient workflow logs

### Recommended directory layout

Use a dedicated artifact root such as:

- `artifacts/review/`

Suggested file naming:

- `artifacts/review/run-<timestamp>.md`
- `artifacts/review/run-<timestamp>.json`
- `artifacts/review/pr-<number>-<timestamp>.md`
- `artifacts/review/pr-<number>-<timestamp>.json`
- `artifacts/review/issue-<number>-<timestamp>.md`
- `artifacts/review/issue-<number>-<timestamp>.json`
- `artifacts/review/index.json`

### Artifact set per review

For each review, save:

1. **markdown report**
   - human-readable retrospective and notes
2. **JSON scorecard**
   - structured and machine-consumable
3. **manifest entry**
   - target identifiers
   - grades
   - paths
   - comment posting result

### Logging and artifact coordination

When a report is generated, log:

- target ID
- score and grade
- main trend flags
- exact output paths

This lets workflow users quickly locate the full report artifact.

### Optional GitHub comment posting

Because the user requested it, the design should support optional concise grade-summary comments on reviewed issues and PRs.

#### Comment policy

- disabled by default in config
- enabled per target type
- concise summary only
- include score, grade, top findings, and a pointer to the full report artifact
- avoid reposting duplicate summaries for the same target and review timestamp

#### Recommended comment shape

```md
## Caretaker Review Summary

- Score: 78 `B`
- Outcome: successful but with elevated churn
- Key strengths: fast detection, complete audit trail
- Key concerns: repeated CI retries, review-loop inefficiency
- Full report artifact: `artifacts/review/pr-123-2026-04-16T120000Z.md`
```

#### Duplicate suppression

Store a marker in `MemoryStore`, for example:

- namespace: `review-agent-comments`
- key: `pull_request:123:2026-04-16T120000Z`

This prevents duplicate comment spam during reruns.

### Interaction with existing orchestrator artifact behavior

The review artifacts should complement, not replace, the existing summary JSON written by `Orchestrator.run`. The new design should add review-specific artifact paths to either:

- `AgentResult.extra`
- and or a future expanded report manifest

This avoids coupling review content to the base run summary schema too early.

## Proposed config additions

Add a dedicated config section, for example:

```yaml
review_agent:
  enabled: false
  mode: scheduled
  lookback_runs: 10
  lookback_days: 30
  artifact_dir: artifacts/review
  save_markdown: true
  save_json: true
  save_manifest: true
  publish_summary_comments: false
  comment_on_prs: true
  comment_on_issues: true
  minimum_comment_score: 0
  use_llm_for_retro: true
```

Additional useful controls:

- `max_history_items_per_review`
- `trend_repeat_threshold`
- `store_review_snapshots_in_memory`
- `include_current_run_in_scheduled_review`

## Summary mapping into `RunSummary`

Avoid overloading the existing `RunSummary` too aggressively at first. Add only coarse review metrics if implementation proceeds.

Recommended future fields:

- `reviews_completed`
- `review_average_score`
- `review_low_scores`
- `review_artifacts_written`

If schema churn should be minimized, keep these in `AgentResult.extra` first and defer `RunSummary` expansion until the feature proves useful.

## Suggested implementation sequence

1. add review config and models
2. implement evidence collection from tracker state and GitHub
3. implement deterministic scoring
4. add `MemoryStore` snapshot read and write support for review history
5. render markdown and JSON artifacts
6. add optional summary comment posting
7. register the agent under a `review` mode and optionally `full`
8. add tests for scoring, artifact rendering, memory trend detection, and comment deduplication

## Key design decisions

### Decision 1: use tracker plus memory, not either alone

`run_history` is good for recent context but too shallow for trend analysis. `MemoryStore` gives the longer historical spine needed for recurring-issue detection.

### Decision 2: deterministic score first, LLM explanation second

Scoring should be reproducible and testable. LLM usage should explain findings and improve the retrospective narrative, not determine the raw grade alone.

### Decision 3: artifact-first outputs with optional comments

The full report belongs in saved artifacts. GitHub comments should remain concise summaries that point readers to the artifact instead of duplicating the entire retro.

### Decision 4: review after work is complete enough to assess

The agent should target completed or stable checkpoints rather than actively mutating workflows. It is an evaluator, not an operator.

## Recommendation

Implement `ReviewAgent` as a new retrospective analysis agent that:

- integrates through the existing `BaseAgent` and registry flow
- reads recent state from `StateTracker`
- reads and writes longer-term review snapshots in `MemoryStore`
- produces markdown and JSON report artifacts
- optionally posts concise score-summary comments on issues and PRs
- uses a stable scorecard schema with explicit retro sections for maintainers, Copilot, and future agents
