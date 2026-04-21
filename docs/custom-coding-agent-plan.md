# Custom coding agent — design + rollout

Caretaker today assigns every coding task — from a one-line ruff fix to a
multi-file feature — to `copilot-swe-agent[bot]`. We pay Copilot seat /
per-task cost even for tasks a deterministic tool-loop could finish in
seconds, and we lose headroom when Copilot is rate-limited or declines
a small task as "not actionable."

Goal of this plan: run small coding work on a caretaker-owned custom
agent (initially the existing Foundry executor, with the door open for
`claude-code-action`), and only fall through to Copilot for tasks that
genuinely need it.

## Non-goals

- Replacing Copilot for complex issues (feature_small, bug_complex,
  refactor). The router escalates to Copilot on size-budget,
  path-denylist, or outright failure.
- Removing the Copilot path. Copilot remains the default provider so
  existing consumer workflows keep working unchanged.
- Building a new model backend. We reuse Foundry's in-process
  tool-loop (already shipped) and plug in other executors behind the
  same interface if we need them later.

## Prior art (what's already in caretaker)

- `ExecutorConfig` + `FoundryExecutorConfig` in
  `src/caretaker/config.py` — `provider: copilot | foundry | auto` is
  the single routing switch.
- `ExecutorDispatcher` in `src/caretaker/foundry/dispatcher.py` — the
  routing seam. Converts `CopilotTask` → `CodingTask`, runs Foundry
  when eligible, falls back to Copilot on escalation or failure. PR
  agent already calls it for CI-fix and review-fix tasks.
- `FoundryExecutor` in `src/caretaker/foundry/executor.py` — runs a
  tool-loop in a git worktree, commits, pushes, and posts the same
  `<!-- caretaker:result -->` markers the Copilot state machine reads.
- Pre- and post-flight size gates in
  `src/caretaker/foundry/size_classifier.py` — task-type allowlist,
  max files (10), max diff lines (400), same-repo-only.
- `IssueDispatcher` accepts a dispatcher parameter but **does not call
  it yet** (explicit TODO at `src/caretaker/issue_agent/dispatcher.py`).

So the pipeline is half-wired: PR-agent dispatches through the router,
issue-agent still hard-codes Copilot assignment.

## Target architecture

```
┌────────────────────────────┐          ┌───────────────────────────┐
│ orchestrator run loop      │          │ GitHub (issues + PRs)     │
│                            │          │                           │
│ pr_agent ─┐                │ ── task ▶│ assign `copilot-swe-agent` │
│ issue_agent─┼── Dispatcher │          │ OR                        │
│ devops_agent ─┘            │ ── task ▶│ receive commit/PR from    │
│                            │          │ custom agent              │
└──────────┬─────────────────┘          └───────────────────────────┘
           │
           ▼
  ┌──────────────────┐
  │ label overrides  │  agent:custom   → force custom
  │ + size gates     │  agent:copilot  → force copilot
  │                  │  agent:quarantine → refuse
  └──────┬───────────┘
         │
         ▼
  ┌─────────────────┐     fallback     ┌─────────────────────────┐
  │ Foundry / custom│ ───────────────▶ │ copilot-swe-agent[bot]   │
  │ executor        │     on fail /    │ assignment + @copilot    │
  │ (inline today)  │     over budget  │ comment                  │
  └─────────────────┘                  └─────────────────────────┘
```

### Routing priority

1. **Label override.** If the issue/PR carries one of caretaker's
   routing labels, that wins (even over `provider: auto`):
   - `agent:quarantine` → dispatch is refused, issue escalated.
   - `agent:copilot` → always Copilot.
   - `agent:custom` → always custom executor (Foundry / claude-code).
2. **Executor config.** `executor.provider` picks the default:
   `copilot` (legacy default), `foundry`, `claude_code` (Phase 2), or
   `auto` (try custom if eligible, else Copilot).
3. **Eligibility gate.** Task-type must be on the allowlist; PR / issue
   must not exceed the size budget (files, lines, path denylist); the
   head ref must be same-repo for PRs; author must not be external
   without the `agent:custom` label.

### Task-type allowlist (Phase 1 defaults)

```python
FoundryExecutorConfig.allowed_task_types = [
  "LINT_FAILURE",
  "FORMAT_FAILURE",
  "REVIEW_COMMENT",
  "DOCSTRING_ADD",
  "IMPORT_SORT",
  "TEST_FIX_TRIVIAL",
]
```

Everything outside this list keeps going to Copilot. Operators can
extend the list per repo via `.github/maintainer/config.yml`.

### Size budget (unchanged from today, documented here for completeness)

| Gate | Default | Rationale |
|---|---:|---|
| `max_files_touched` | 10 | Custom agents perform poorly on cross-file refactors (SWE-Bench Pro: <25% accuracy on multi-file >100 LOC patches). |
| `max_diff_lines` | 400 | Keeps diffs reviewable; anything larger should be human-scoped. |
| `route_same_repo_only` | true | Avoids fork token / push permissions edge cases. |
| `write_denylist` | `.github/workflows/`, `.caretaker.yml`, `pyproject.toml`, `setup.py`, `setup.cfg`, etc. | Never let a non-human agent edit CI / release / dependency files. |

### Path allowlist (Phase 2)

In addition to the denylist we'll want an allowlist for the smallest
tasks — e.g. lint/format may edit `src/**/*.py` only, never `infra/`,
`docs/`, `migrations/`. Phase 2 adds `path_allowlist: list[str]` gated
by task type.

## Phased rollout

### Phase 1 — wire it up (this PR, #???)

- Expand `allowed_task_types` defaults to include trivial CI-break
  types (see above).
- Add label-based routing overrides (`agent:custom`, `agent:copilot`,
  `agent:quarantine`) to `ExecutorDispatcher.route()`.
- Complete the `IssueDispatcher` → `dispatcher.route()` wiring for
  `BUG_SIMPLE` classifications that pass size gates.
- Tests + docs.

No new infra. Runs inside the existing `caretaker run` invocation, i.e.
the same GitHub Actions runner that already does orchestration.

### Phase 2 — additional executors *(shipped)*

- `ClaudeCodeExecutor` added in `src/caretaker/claude_code_executor.py`.
  Conforms to the same `async run(task, pr) -> ExecutorResult` shape
  as `FoundryExecutor`, so the dispatcher routes to either with no
  special-casing.
- Hand-off model (simpler than running the upstream action inline):
  executor posts a structured comment that carries `@claude` +
  caretaker's task details, then applies a configurable trigger label
  (`claude-code` by default). The upstream
  [`anthropics/claude-code-action`][cca] workflow picks up the
  mention / label and produces the fix asynchronously; caretaker's
  existing `<!-- caretaker:result -->` state machine reads the result
  commit back.
- Config: `executor.provider = "claude_code"` plus a new
  `claude_code` block (`enabled`, `trigger_label`, `mention`,
  `max_attempts`).
- Attempt cap: executor counts prior hand-offs on the PR via a
  marker comment (`<!-- caretaker:claude-code-handoff -->`); beyond
  `max_attempts` it escalates to Copilot to avoid ping-pong if the
  upstream action can't complete the work.
- Dispatcher extended with:
  * `RouteOutcome.CLAUDE_CODE` — successful hand-off.
  * `provider = "claude_code"` support.
  * `provider = "auto"` now tries Claude Code when Foundry is
    ineligible but Claude Code is enabled, before falling to Copilot.
  * `agent:custom` label honours whichever custom executor is
    currently active (Foundry first if both configured, else Claude
    Code).
- Tests: 12 new cases covering config defaults, comment + label
  application, label-failure graceful degradation, attempt cap,
  dispatcher routing through every new path.

### Phase 3 — scale out onto AKS *(shipped)*

Live pieces:

- `infra/k8s/caretaker-agent-worker.yaml` — ServiceAccount +
  namespace-scoped Role (`create` on `jobs`) + RoleBinding + a
  template Job with securityContext / resource limits / TTL /
  activeDeadline. Cloned per dispatch.
- `src/caretaker/k8s_worker/launcher.py::K8sAgentLauncher` — the
  worker launcher. Pure-function `build_job_manifest()` synthesises
  the Job spec from config + dispatch payload; `dispatch()` calls
  `BatchV1Api.create_namespaced_job` and persists a Redis dedupe
  pointer. The `kubernetes` Python client is an optional dependency
  (`k8s-worker` extras group) — the launcher raises a structured
  `K8sLauncherError` instead of `ImportError` when it's missing so
  callers return 503 cleanly.
- `src/caretaker/k8s_worker/api.py` — admin endpoints:
  * `POST /api/admin/agent-tasks {repo, issue_number, task_type,
    image?}` → spawn (or return deduped) Job.
  * `GET  /api/admin/agent-tasks?limit=50` → list recent worker Jobs.
- MCP backend wiring in `caretaker.mcp_backend.main`: the endpoints
  only register when `executor.k8s_worker.enabled = true`, so the
  backend doesn't need the optional k8s package unless the feature is
  on.
- `K8sAgentWorkerConfig` on `MaintainerConfig.executor` (`enabled`,
  `namespace`, `image`, `service_account`, `template_job_name`,
  `name_prefix`, `dedupe_ttl_seconds`, `ttl_seconds_after_finished`,
  `active_deadline_seconds`). Off by default.

Dedupe model: `caretaker:agent-dispatch:<repo>#<num>:<task_type>` ⇒
Job name, with configurable TTL (default 900s). Protects against
duplicate-submit storms if the admin UI retries.

Azure Container Apps Jobs considered and rejected: caretaker already
runs on AKS, reusing the cluster is one fewer Azure surface to
operate. Revisit if we ever need cross-region runners.

### Phase 4 — consumer-side opt-in workflow template

For consumers that don't run the full caretaker backend (the majority
of the fleet), we ship a GitHub Actions workflow template they can
drop into their repo:

- `setup-templates/templates/workflows/agent-custom.yml`
- Triggers on `issues.labeled` where label == `agent:custom`.
- Checks out, installs caretaker, runs the Foundry executor against
  the issue, opens a draft PR.
- Uses a caretaker-minted GitHub App installation token (or the
  repo's existing `GITHUB_TOKEN` — documented tradeoffs).

This is the "no backend required" path. Documented alongside the
existing `maintainer.yml`.

## Security model (per the research pass)

- **Identity.** A caretaker GitHub App; per-run installation tokens
  scoped to the single target repo. PAT only for the `copilot-swe-agent[bot]`
  assignment fallback that requires user-authored tokens.
- **Sandboxing.** Executor runs in a disposable container / worktree.
  Read-only root filesystem except workspace. Egress allowed only to
  `api.github.com`, `api.anthropic.com` (if claude-code), the package
  registries for the language in scope.
- **Prompt-injection hardening.** Issue bodies, PR bodies, comments,
  file contents, and CI logs are **untrusted input**. The executor
  must never act on "run this shell command" instructions sprouting
  from those sources. Writes go through the `<!-- caretaker:result
  -->` marker channel — anything else gets refused by the receiving
  state machine. Mirrors the `safe-outputs` pattern in
  [github/gh-aw][ghaw].
- **Branch protection.** Agent-authored PRs land as **draft**, require
  CI green, and require one human review. Agent identity cannot
  approve (GitHub already enforces this for Copilot — we mirror it).
- **Quarantine.** A single `agent:quarantine` label hard-stops
  dispatch on that issue / PR. Charlie sweeps stuck items into the
  weekly human-action digest.

## Observability

- Each dispatch writes a row to caretaker's `RunSummary.docs_prs_*` +
  the new `agent_router_decisions` counter (`foundry`,
  `copilot`, `fallback`, `refused`).
- Every agent-authored commit carries a causal marker (existing B3/F3
  chain-audit work) so the admin dashboard shows full provenance.
- Fleet-registry heartbeat (just landed) will include the router mix
  once this change is in, so we can see per-repo custom-vs-copilot
  ratios.

## Open questions

- **GitHub App vs PAT.** The Copilot assignment endpoint still
  requires a user-authored token. We keep `COPILOT_PAT` for that one
  path; everything else should migrate to the App. Tracked separately
  under `docs/github-app-plan.md`.
- **Path allowlist per task type.** Phase 2 — worth adding but not
  urgent enough to block Phase 1.
- **`claude-code-action` auth.** Foundry backend option works today;
  we need to decide if caretaker provisions Foundry creds for every
  consumer or if each consumer supplies their own.

## References

- [Foundry Hosted Agents](https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/hosted-agents) — Azure-native execution surface.
- [anthropics/claude-code-action][cca] — reference GH Action
  integration; Foundry backend supported.
- [github/gh-aw][ghaw] — safe-outputs pattern for prompt-injection
  hardening.
- [SWE-Bench Pro](https://arxiv.org/abs/2410.03859) — accuracy
  degradation above ~100 LOC / multi-file, informs our size budget.
- Caretaker's own `src/caretaker/foundry/dispatcher.py` — the routing
  seam we extend.

[cca]: https://github.com/anthropics/claude-code-action
[ghaw]: https://github.com/github/gh-aw
