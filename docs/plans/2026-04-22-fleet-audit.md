# Caretaker Fleet Audit — Consumer-Repo Effectiveness (as of 2026-04-22)

## Executive verdict

Of five consumer repos, only **two** are meaningfully served by caretaker today: `Example-React-AI-Chat-App` (the most active dogfooding target) and `python_dsa` (the newest, still recovering from a CI-format storm). `kubernetes-apply-vscode` is marginal. `flashcards` and `audio_engineer` are effectively non-functional — `audio_engineer`'s `Caretaker` workflow has had **a 14% success rate over its last 50 runs** (7 success, 43 failure) and has not produced a healthy run on `main` since 2026-04-17.

The fleet exposes the same handful of root causes repeatedly: the `maintain` job exits non-zero at the end of otherwise-productive runs (breaking every consumer that relies on `self-heal-on-failure` as a guardrail), the `GITHUB_TOKEN` used by caretaker lacks several of the permissions its agents assume (assignees on restricted repos, Dependabot/code-scanning alerts, PR creation), and caretaker's Copilot-driven upgrade path regularly orphans its own PRs in an "awaiting requirements" state.

---

## Per-repo scorecard

### 1. `ianlintner/audio_engineer` — **mostly broken**

- **50 runs / 7 success / 43 failure** (14% success). All 6 Dependabot PRs opened 2026-04-22 triggered `Caretaker` runs that failed; the most recent `schedule`-equivalent push run was 24762617294 (also failed).
- Workflow is **stuck on a broken state**: every push to `main` for a week has failed the `Caretaker` workflow, yet there are **no caretaker-authored issues** under this workflow name beyond orchestrator state (`[Maintainer] Orchestrator State` #11) — the self-heal and escalation-digest paths are not firing here. This strongly suggests the failure is happening **before** the orchestrator runs (likely a secret-missing / install-step failure during the workflow bootstrap), so none of caretaker's own telemetry agents get a chance to run.
- 22 Copilot PRs (21 merged, 1 closed-unmerged) and 44 Dependabot PRs (19 merged, 3 still open, ~22 closed-unmerged) — Copilot/Dependabot automation itself is working, but **caretaker isn't actually merging/shepherding them**; the human operator or classic GH auto-merge is.
- No `caretaker:owned`, `caretaker:self-heal`, or `maintainer:escalation-digest` labels on any current issue — caretaker can't even signal its own failures on this repo.
- Verdict: **mostly broken**. The workflow itself is red; every run is wasted CI minutes. Nothing user-visible about the repo improves because of caretaker.

### 2. `ianlintner/python_dsa` — **partially working**

- **81 runs / 48 success / 14 failure / 19 cancelled** (59% success; cancellation is mostly the concurrency-group pattern working as intended on burst check_suite events).
- The orchestrator `[Maintainer] Orchestrator State` issue (#23) is actively being updated — **146 comments**, all `github-actions`, mostly `caretaker:run-history` and `caretaker:orchestrator-state` markers — and the dispatch-guard is correctly short-circuiting those.
- Escalation is active but **crude**: issue #30 ("CI failure on main: maintain (unknown)") has been open since 2026-04-17; issue #31 (W16 digest) is stale; PRs #39, #42, #43 were all opened by Copilot on 2026-04-20 to fix the same trailing-newline problem and are still open ("caretaker:owned", `maintainer:escalated`, 60% readiness, "Reviews approved 0%"). On PR #42 all CI checks are now green (CLEAN state) and it **still sits unmerged** — caretaker's readiness gate blocks on human approval that will never come.
- The v0.10.1 upgrade PR #45 is in the same state. Issue #44 ("Upgrade to v0.10.1") has Copilot assigned but the PR loops on the same readiness-score comment without the merge bot ever taking action.
- Verdict: **partially working**. Instrumentation and CI self-healing fire; the "last mile" merge/approve step does not.

### 3. `ianlintner/kubernetes-apply-vscode` — **partially working**

- **15 runs / 11 success / 0 failure / 4 action_required**. Cleanest success rate on paper, but volume is low (mostly PR events).
- Actively escalating: #16 ("Caretaker self-heal: Unknown caretaker failure: "), #10/#11 (CI-failure twins for `self-heal-on-failure` and `maintain`), #18 (`[Maintainer] Upgrade to v0.10.2`) all open with no assignees on the escalations despite `maintainer:assigned` labels being applied — classic label-applied-without-actual-assignee.
- Copilot upgrade loop on display: PR #16 (v0.10.1) and PR #17 (EOF-newline fix) both sit at "Monitoring — awaiting requirements", `action_required`, readiness 20–40%. The "add trailing newline" Copilot PR chain is the exact same bug pattern as python_dsa and flashcards, suggesting the caretaker release-sync tool produces a config file without a trailing `\n` and every downstream pre-commit hook chokes on it.
- Verdict: **partially working**. Caretaker correctly detects and files, but leaves the work undone.

### 4. `ianlintner/flashcards` — **mostly broken**

- **14 runs / 6 success / 8 failure** (43% success). Scheduled run on 2026-04-21 (ID 24712826996) failed at the final step despite the orchestrator logging a full run — the `upload-artifact` step then fails ("No files were found... .caretaker-memory-snapshot.json") and `Process completed with exit code 1`. That non-zero exit masks the fact that the orchestrator itself largely succeeded.
- Inside that same log: `POST /issues/18/assignees → 403 Forbidden` (cannot assign Copilot/`ianlintner` on issue #18), `dependabot/alerts → 403`, `code-scanning/alerts → 403`, `secret-scanning/alerts → 403`, `POST /pulls → 403` on the docs changelog PR. Five separate integration-permission gaps, each silently swallowed by `caretaker.security_agent` / `caretaker.docs_agent`.
- The orchestrator still manages to update escalation-digest #9 (W16) — **but W16 was 2 weeks stale**, listing self-heal issues #7 and #16 that the escalation agent itself created. Caretaker is digesting its own exhaust.
- 1 Copilot upgrade PR merged recently (#5 v0.5.2, 2026-04-17); one PR merged via human; no Dependabot traffic.
- Verdict: **mostly broken**. Every scheduled run fails its final step, every PR path is 403.

### 5. `ianlintner/Example-React-AI-Chat-App` — **working well**

- **100 runs / 71 success / 22 cancelled / 6 failure / 1 action_required** (71% success; cancellations are the concurrency-group pattern). 62 of 100 runs are `check_suite` (webhook-driven from dependent CI completions), which is exactly the reactive pattern caretaker claims.
- The caretaker-owned Dependabot rollups (PR #195 "npm_and_yarn group across 4 directories", 19 updates; PR #192 across 3 dirs, 20 updates) carry the `caretaker:owned` label and are staying green — but **no merges** yet. UNSTABLE state indicates failing CI.
- Self-heal loop is observable but controlled: issues #134, #178, #199 all "Unknown caretaker failure: Process completed with exit code 1." #134 and #178 closed (one by stale, one by digest); #199 created 6 hours ago. Self-heal is producing duplicate issues every time the orchestrator final-step fails — same artifact-upload non-zero-exit bug as flashcards.
- Escalation digest #177 (W17) is well-formed.
- Verdict: **working well** in absolute terms — but a large share of that "success" is caretaker doing nothing because the PRs it watches are working under their own steam.

---

## Patterns across the fleet

**P1 — "Final-step false-failure" drives self-heal storms.** On at least `flashcards`, `Example-React-AI-Chat-App`, and intermittently `python_dsa` / `kubernetes-apply-vscode`, the orchestrator logs a clean run but exits 1 because `upload-artifact` cannot find `.caretaker-memory-snapshot.json`, or because `Run completed with 1 errors` is escalated to job failure. This fires `self-heal-on-failure`, which opens a `Caretaker self-heal: Unknown caretaker failure: Process completed with exit code 1.` issue with an empty error body (because the error was swallowed). The same issue gets re-opened each scheduled run until the weekly digest closes or escalates it. Evidence: `flashcards` run 24712826996; `Example-React` issues #134 / #178 / #199.

**P2 — Permission-starved integration token.** caretaker's agents assume scopes the workflow `GITHUB_TOKEN` doesn't always have: `Dependabot alerts` (`flashcards` 403), `code-scanning/alerts` (403), `secret-scanning/alerts` (403), `POST /pulls` for docs changelog (403), `POST /assignees` for Copilot (403). Each agent logs a warning and moves on — no user-visible signal that half the surface area is dead. Evidence: `flashcards` run 24712826996 log.

**P3 — Trailing-newline Copilot fan-out.** When caretaker asks Copilot to bump its own version, the templated `maintainer.yml` lands without a final `\n`, the consumer's `end-of-file-fixer` pre-commit hook fails, caretaker's devops agent files a CI-failure issue, assigns Copilot to produce another PR to add the newline, and that PR also doesn't merge itself. Three repos (`python_dsa`, `kubernetes-apply-vscode`, `flashcards`) show this exact chain within 48 hours of each other. Evidence: `python_dsa` PRs #39 / #42 / #43, `kubernetes-apply-vscode` PR #17, matching `maintainer:escalated` labels.

**P4 — Readiness-gate dead end.** Caretaker's PR agent computes a readiness score (0/20/40/60/80%) and blocks on "Reviews approved = 0%" for its own PRs. On a single-maintainer repo with no reviewers, that gate can never clear. PRs on `python_dsa` #42 sit at `MERGEABLE / CLEAN` with all checks green and still do not merge. This is a policy mismatch, not a bug — but it means caretaker **cannot actually complete its headline job** (merging its own upgrade PRs) on any solo repo.

**P5 — Escalation-digest is self-referential.** In `flashcards` #9, the digest lists the self-heal issues and CI-failure issues caretaker itself produced. There is no real human-actionable content — it's an internal audit log being re-surfaced as an actionable weekly report. Same on `kubernetes-apply-vscode` #19 (W17 digest).

**P6 — Dispatch-guard works correctly.** On `audio_engineer`, `issue_comment` events from bots and caretaker-marker comments are short-circuited at the guard step (no `maintain` job runs). No evidence of webhook infinite loops. The guard regex is straightforward and I did not see it produce false positives or negatives in sampled events.

**P7 — Scheduled vs. webhook run split.** `Example-React` runs are 62% `check_suite`, 30% `pull_request`, 1% `schedule` — genuine reactive maintenance. `audio_engineer` is 54% `push` (all failing) and 0% `schedule`/`check_suite`, indicating the `cron` and check_suite hooks aren't even landing on that repo. Likely caretaker workflow is disabled on schedule because the job fails so consistently that GitHub has auto-disabled it — worth confirming in repo settings.

**P8 — `audio_engineer` is ghosted.** No successful `schedule` run on record, no `caretaker:*` labels on any issue, `Orchestrator State` issue not updated since setup. That repo isn't "partially working" — caretaker has been dark there for a week and nobody noticed because the digest never got written.

---

## Ranked issues (impact × frequency)

| # | Issue | Repos | Evidence | Hypothesis |
|---|---|---|---|---|
| 1 | Orchestrator exits 1 on artifact-upload / residual errors, triggering duplicate self-heal issues on every schedule | flashcards, Example-React, kubernetes-apply-vscode | flashcards run 24712826996 (`No files were found with the provided path: .caretaker-memory-snapshot.json`); Ex-React issues #199/#178/#134 | Memory snapshot is conditionally written; absent file shouldn't be fatal — set `if-no-files-found: ignore` AND make orchestrator exit 0 when all agent errors are in known-failure buckets (403s etc.) |
| 2 | Caretaker workflow fully red on `audio_engineer` for a week | audio_engineer | Runs 24754140054, 24762617294, etc.; no `caretaker:self-heal` issue opened (meta) | Failing before orchestrator bootstrap — probably missing `workflows: write` / missing `OPENAI_API_KEY`/OAuth2 secret, or release-install step 404. Telemetry silence confirms it's pre-orchestrator. |
| 3 | Readiness gate requires human approval that never arrives on solo repos | python_dsa, kubernetes-apply-vscode, Example-React | python_dsa PR #42 (clean, 60% readiness, `maintainer:escalated`), #45; k-apply #16/#17 | Hardcoded `reviews_approved >= 1` in readiness heuristic; should be configurable or LLM-judged for self-owned PRs |
| 4 | Copilot upgrade PRs produce `maintainer.yml` without trailing newline, cascading into secondary "add EOF newline" PRs | python_dsa, kubernetes-apply-vscode, flashcards | python_dsa #39/#42/#43, k-apply #17 | Template writer in caretaker doesn't round-trip YAML with `\n` at EOF; fix in the release-sync tool |
| 5 | Token lacks several scopes the agents quietly assume | flashcards (confirmed), likely audio_engineer | flashcards log: `403 Forbidden` on `dependabot/alerts`, `code-scanning/alerts`, `secret-scanning/alerts`, `pulls`, `assignees` | Workflow `permissions:` block insufficient, or `security_events: read` / `actions: write` missing; agents need to be scope-gated up front and skip loudly, not quietly |
| 6 | Escalation digest lists only caretaker-internal issues | flashcards #9, k-apply #19, Example-React #177 | See bodies (self-heal/CI-failure-of-caretaker) | Digest classifier should exclude issues whose author is `app/github-actions` unless they're user-facing (e.g., `dependencies:major-upgrade`) |
| 7 | Self-heal storm: 7+ duplicate `CI failure on main: self-heal-on-failure (unknown)` issues in 2 minutes | audio_engineer | Issues #33/#36/#39/#43/#45/#46/#48 all opened 2026-04-15 02:53–02:55 | Claims to have a "self-heal storm cap (5/hour, 20/day)" in v0.10.0 release notes, but the cap key didn't apply here because repo was on pre-0.10.0. Still, it slipped through. |
| 8 | Orchestrator-state tracking issue balloons comment history (146 on python_dsa #23) | python_dsa, others | 146 comments on a single issue | Two separate markers (`caretaker:orchestrator-state`, `caretaker:run-history`) each append instead of edit-in-place on some runs — the run-history text claims "edited in place on every run", but the pattern here is append-then-patch. Worth a dedupe. |
| 9 | `maintainer:assigned` label applied without an actual GitHub assignee | flashcards #9, Example-React #177 | `assignees: []` with label present | Label-assign is decoupled from the `POST /assignees` call that 403s (see #5); label still lands. |
| 10 | Dependabot PRs with `caretaker:owned` never merge despite green CI | Example-React #192, #195 | `mergeable: MERGEABLE UNSTABLE` — CI actually failing on the update bundle itself | Bundled 19-package dependabot groups break on the real build; caretaker labels them owned but has no path to split/fix individual breakages. Design gap: caretaker can't bisect a grouped Dependabot PR. |
| 11 | Dry-run triage block added via `claude-code` PR on all 5 repos 2026-04-22 but unmerged | all 5 | PR bodies "enable fleet-registry heartbeat with OAuth2", `labels: [claude-code]` | New feature rollout in flight; will land once OAuth2 creds are accepted |
| 12 | Concurrency cancellations account for 20–30% of `Example-React` run count | Example-React, python_dsa | 22/100 `cancelled` on Ex-React | Intentional (concurrency: caretaker cancel-in-progress), but skews success-rate metrics; dashboards should filter. |
| 13 | `kubernetes-apply-vscode` shows `action_required` conclusion — missing approval for first-time workflow runs from bots | k-apply | 4/15 runs `action_required` | GH settings: "Require approval for first-time contributors" blocks dependabot/copilot workflow runs until manually approved. Config gap, not caretaker bug. |

Roughly: 1–5 are genuine bugs, 6–10 are design questions, 11–13 are config gaps.

---

## Agentic-vs-bizlogic observations

**Places where hand-written heuristics are doing LLM work (badly):**

- **Readiness score is a hardcoded weighted sum** (`draft 0%, automated feedback 20%, reviews 0%, CI 40%` = 60%). This is a classifier job — an LLM with PR context ("is this my own upgrade PR on a solo repo?", "did the author already say 'LGTM'?") would give a much better answer than a linear formula that can never clear a "solo repo" PR.
- **Self-heal issue classifier lumps everything non-matching into "Unknown caretaker failure"** with an empty error body. An LLM reading the last 200 log lines would correctly bucket this as "upload-artifact missing file — benign" vs. "actual orchestrator crash", dramatically shrinking the self-heal volume.
- **Escalation-digest grouping is label-based**, so it pulls in caretaker-internal self-heal noise. An LLM summarizing "what actually needs human attention this week" would trivially omit the meta-noise.

**Places where LLM is handling what should be deterministic:**

- **Dispatch-guard should stay regex.** The marker check is correctly deterministic; delegating it to an LLM would reintroduce webhook loops under model flakiness. Current implementation is correct — keep it.
- **Copilot-driven "add EOF newline" PRs are LLM work for a 1-line deterministic fix.** Asking Copilot to add a trailing newline to a file produces a 4-hour round-trip. A trivial bin/fix should run deterministically in caretaker itself and open a self-authored PR (no Copilot invocation).
- **Version-pin bump.** Caretaker asks Copilot to "apply this upgrade" (issue #35 on python_dsa); the only file change is `.github/maintainer/.version`. Same as above — this is a `sed` job, not a Copilot task.
- **Readiness-comment re-rendering.** Caretaker posts the same "Monitoring — awaiting requirements / 60% / Blockers" block on every run for every PR. Formatting is deterministic templating; the work done per render is negligible — but it's firing through an LLM path in some agents, wasting tokens.

**Direction of travel:** move the `readiness`, `escalation-digest content`, and `self-heal classification` to LLM. Move the `version-bump`, `EOF-newline fix`, and `readiness-comment render` to deterministic code and out of Copilot.

---

## Data gaps

Things I could NOT determine from GitHub alone:

- **Secret / OAuth2 state per repo.** Cannot confirm which repos have `OPENAI_API_KEY`, `OAUTH2_CLIENT_ID`, `OAUTH2_CLIENT_SECRET`. `audio_engineer`'s silent bootstrap failure is most easily explained by missing secrets, but I can't prove it without admin access to repo settings or the Mongo audit log showing which run attempted an OAuth exchange.
- **Mongo audit-log timing.** Run logs show that `state.audit_log` records "audit" events, but the actual event payload (the `caretaker:causal` chain) is not surfaced in GitHub comments — would need the caretaker admin dashboard or Mongo to reconstruct the decision graph.
- **Neo4j relationship graph.** Whether caretaker currently believes the 5 repos are in one fleet (and is propagating state across them) is invisible from GitHub. The recent `fleet_registry.oauth2` PR fan-out suggests this is in progress and not yet wired up.
- **Token scope at install time.** GitHub run logs don't echo the effective permissions of `GITHUB_TOKEN`; the 403s on `flashcards` could be scope gaps, org-level restrictions, or feature-disabled-on-repo (e.g., Dependabot alerts off on a public repo). Distinguishing those requires the repo settings API per repo, not the run log.
- **Pre-orchestrator failure mode on audio_engineer.** Without `--log-failed` content (the logs return 404 — GH may have rolled them off) I can only infer from the run metadata. Would need to trigger a fresh run with debug logging on or pull from Actions' raw log storage.
- **LLM call budget / cost per run.** The log shows `LLM not available — analysis features disabled` on `flashcards`, meaning that deployment is running **without any LLM integration at all**. Which features are degraded by that, and whether the other repos are in the same state, can't be told without per-repo config dump.

---

**Bottom line.** Caretaker has real signal on one repo (Example-React), mixed signal on two (python_dsa, kubernetes-apply-vscode), and effectively zero signal on two (flashcards, audio_engineer). The three dominant failure modes — spurious non-zero exit triggering self-heal, token-scope 403s, and a readiness gate that can never clear on solo repos — are all fixable in the caretaker codebase without any consumer-side changes, and would move the needle more than any new agent.
