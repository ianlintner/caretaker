# Skill: caretaker-qa-cycle

## Purpose

Run a structured QA test cycle of caretaker against the live
[ianlintner/caretaker-qa](https://github.com/ianlintner/caretaker-qa)
testbed. Use this when validating a release end-to-end (especially
releases that change orchestrator/PR-agent behavior, autonomy posture,
or the merge-authority surface).

This is the **live-fire layer** described in
`docs/plans/2026-04-25-qa-orchestration-test-plan.md` §4.5. Unit and
state-machine tests cover individual transitions; this skill exercises
the real LLM, real GitHub permissions, real webhook timing, and real
multi-actor concurrency by driving caretaker through purpose-built
scenarios on a real repo.

## Capabilities

- Decide whether a release warrants a live-fire QA cycle
- Author QA scenario issues that exercise specific behaviors
- Fast-forward the testbed's pin to the release under test
- Watch a scheduled run and assert against the documented invariants
- Triage findings and either fix in caretaker or record as a known gap

## When to Use

Trigger this skill when **any** of the following are true:

- A release touches `pr_agent/`, `orchestrator/`, `pr_reviewer/`,
  `merge_authority/`, `executor/`, or the readiness check publisher
- A release changes default config values (autonomy / merge / review)
- A release introduces a new external write (GitHub API call, label,
  comment marker, check-run conclusion)
- An incident's root-cause was not catchable by unit tests alone (i.e.
  required event ordering, idempotency, or cross-agent state to surface)
- The user asks to "test caretaker", "QA the release", "exercise
  caretaker against caretaker-qa", or similar

Do **not** trigger for purely internal refactors with no observable
behavior change, or for doc-only PRs.

## Prerequisites

- `gh` authenticated as a user with write access to both
  `ianlintner/caretaker` and `ianlintner/caretaker-qa`
- Latest release tag exists (`gh release list -R ianlintner/caretaker`)
- caretaker-qa workflow secrets present: `COPILOT_PAT`,
  `ANTHROPIC_API_KEY` (or `AZURE_AI_API_KEY` + `AZURE_AI_API_BASE`),
  optionally `CARETAKER_FLEET_SECRET` / OAuth2 fleet creds

## QA cycle (5 steps)

### 1. Identify the release surface

Before writing scenarios, identify what behavior changed:

```bash
# What's the latest tag?
gh release list -R ianlintner/caretaker --limit 5

# What did it touch?
gh pr list -R ianlintner/caretaker --search "merged:>=$(date -v-7d +%F)" \
  --json number,title,files --limit 20

# For a specific PR:
gh pr view 604 -R ianlintner/caretaker --json files --jq '.files[].path'
```

Map each changed file to the user-observable behavior it controls. The
scenarios in step 3 should exercise those behaviors — not the code
paths, but the **outcomes** an operator would notice.

### 2. Verify the testbed is current enough to exercise the release

```bash
# Pinned version on caretaker-qa:
gh api /repos/ianlintner/caretaker-qa/contents/.github/maintainer/.version \
  --jq '.content' | base64 -d
```

If the pin is older than the release under test, the testbed is not
running the new code at all. Two options:

- **Fast-forward** (recommended for QA cycles): open a PR bumping
  `.github/maintainer/.version` to the release tag. The bump PR itself
  is a live test of the autonomy / advisory-merge surface — if the
  fixed pr-readiness check would have blocked it, that's the regression
  signal.
- **Honor the upgrade chain**: let the existing
  `[Maintainer] Upgrade to vX.Y.Z` issues drive the bump organically.
  Slower but tests the upgrade agent path. Use this if the release
  changed upgrade-agent semantics.

### 3. Author scenario issues in caretaker-qa

Each scenario is one GitHub issue in caretaker-qa with a
`<!-- caretaker:qa-scenario -->` HTML comment marker (used as a grep
key) and a `<!-- scenario-NN: <slug> -->` second marker.

Use the existing scenarios as a template:

| # | Slug | Tests |
|---|------|-------|
| 04 | `stale-issue` | stale_agent labels + closes after window |
| 05 | `fixes-cascade` | issue closes when linked PR merges |
| 06 | `xss-payload` | sanitize_input guardrail |
| 07 | `deceptive-link` | filter_output guardrail |
| 09 | `idempotency-comment` | duplicate triage comment dedup |
| 10 | `empty-pr-body` | triage close test |
| 11 | `azure-ai-prompt-cache` | prompt cache works through Azure AI |

Reserve the next free number(s) and write each scenario as:

```markdown
## QA Scenario NN — <one-line title>

**Release validated:** vX.Y.Z (PR #NNN)

**Setup:** <what state must exist for the test, and any one-time
configuration steps>

**Expected caretaker behavior:**
1. <observable step 1>
2. <observable step 2>
3. <observable step 3, ideally referencing a metric or check-run conclusion>

**Failure modes the scenario catches:**
- <regression mode 1>
- <regression mode 2>

**How to verify:**
```bash
# concrete commands the QA reviewer can paste to confirm
gh api /repos/ianlintner/caretaker-qa/actions/runs ...
```

<!-- caretaker:qa-scenario -->
<!-- scenario-NN: <slug> -->
```

A scenario is good if a reasonable reviewer can read it and decide
PASS/FAIL by inspection — no caretaker-internal knowledge required.

### 4. Trigger a run and watch the invariants

```bash
# Manually dispatch the caretaker workflow:
gh workflow run maintainer.yml -R ianlintner/caretaker-qa

# Watch the latest run:
gh run watch -R ianlintner/caretaker-qa $(gh run list \
  -R ianlintner/caretaker-qa --workflow maintainer.yml --limit 1 \
  --json databaseId --jq '.[0].databaseId')

# Inspect the run's artifacts (memory snapshot, scope-gap, digest):
gh run download <run-id> -R ianlintner/caretaker-qa
```

For the **server-side dispatch path** (PR #621 onwards) the caretaker
run no longer happens in the consumer's GitHub Actions; it happens in
the backend after a real webhook. Trigger it by opening a real PR or
issue (or pushing to `main`) on caretaker-qa, then watch for caretaker's
side effects directly on the issue/PR:

```bash
PR=66  # the verification PR you just opened
# Comment markers (one per agent kind, no duplicates):
gh pr view "$PR" -R ianlintner/caretaker-qa --json comments \
  --jq '.comments[].body | match("<!-- caretaker:[a-z0-9:_-]+").string' | sort | uniq -c
# Reviews (state, author, SHA):
gh pr view "$PR" -R ianlintner/caretaker-qa \
  --json reviews,headRefOid \
  --jq '{sha: .headRefOid, reviews: [.reviews[] | {state, author: .author.login, at: .submittedAt}]}'
# Readiness check-run conclusion at the head SHA:
gh api "repos/ianlintner/caretaker-qa/commits/$(gh pr view "$PR" -R ianlintner/caretaker-qa --json headRefOid -q .headRefOid)/check-runs" \
  --jq '.check_runs[] | select(.name == "caretaker/pr-readiness") | {status, conclusion}'
```

Prefer the REST `gh pr view --json comments` path over a GraphQL
`pullRequest.comments.nodes[].body` filter — the GraphQL response is
fine but the documented `--jq match("<!-- caretaker:…")` against it is
flaky when the body contains nested HTML-comment markers (e.g. inside
fenced code blocks describing the convention itself). The REST form
above survives both shapes.

Cross-check against the **invariants** in
`docs/plans/2026-04-25-qa-orchestration-test-plan.md` §9. The most
load-bearing for autonomy releases are:

- **No double-approve same SHA** — `count(create_review event=APPROVE
  for pr=N at sha=S) <= 1`
- **Caretaker PRs only auto-approve** — head ref must start with
  `caretaker/`, `claude/`, or `copilot/`
- **Transient-only errors do not fail the run** — exit 0 if all errors
  in `report.errors` are `RATE_LIMIT` or `TRANSIENT_NETWORK`
- **Advisory mode never publishes `failure`** — the pr-readiness
  check-run conclusion is `success | neutral | skipped`, never
  `failure`, when `pr_agent.merge_authority.mode == advisory`
- **Idempotency** — replay the same scheduled run on the same head SHA
  → no new GitHub writes

### 5. Triage findings

For each scenario that doesn't pass:

1. Decide if the bug is in caretaker (fix in main) or in the testbed
   (fix in caretaker-qa).
2. If in caretaker: open a PR with a regression unit test in
   `tests/test_pr_agent/` or `tests/test_orchestrator/` **first**,
   then the fix. Reference the QA scenario number in the PR.
3. If in caretaker-qa: open a PR; do not weaken the scenario unless
   the expected behavior was wrong.
4. Append the finding to `docs/qa-findings-YYYY-MM-DD.md` (one file
   per cycle) — this is the long-term scoreboard.

Always finish a cycle by:

- Closing scenarios that pass (label `caretaker:qa-passed`)
- Leaving open the ones that surfaced bugs until the fix lands
- Updating the pinned version in caretaker-qa to whatever release
  shipped the fix

## Common patterns

### Pattern: testing a "PR blocking deadlock" fix (advisory → neutral)

When a release fixes a PR-blocking class of bug (e.g. PR #604), the
scenario must exercise the **branch-protection-required-check chain**,
not just the conclusion publisher in isolation:

1. Open a PR on caretaker-qa with a head branch matching `caretaker/*`
   that fails one transient blocker (e.g. CI red on a flaky test).
2. Configure branch protection on `main` to require the
   `caretaker/pr-readiness` check (one-time, document in the
   scenario).
3. Schedule two consecutive caretaker runs.
4. Assert the PR is not blocked indefinitely: either it merges
   (advisory mode → neutral, no block) or the operator gets a clear
   `gate_only` failure with an explicit signal.

### Pattern: testing a parent-threading / causal-event fix

Releases that change the causal-event store (e.g. v0.22.0 PR #601)
need a scenario where **two agent runs share state via a parent_id**:

1. File an issue in caretaker-qa that triggers `issue_agent`.
2. After issue_agent dispatches a fix to copilot, capture the
   `CausalEvent.parent_id` from the memory snapshot artifact.
3. On the next scheduled run, assert that `pr_agent`'s evaluation of
   the resulting PR has `parent_id` matching the issue's causal id.

If parent_id is null on the second run, the persistence path is broken
even if unit tests pass.

### Pattern: regression cassette from a real incident

When an incident surfaces, the **post-fix QA scenario** is the
incident's own event log replayed on the new code. Pull the run id
from the incident, dump the event log via
`CARETAKER_RECORD_CASSETTE=path` (when available), and commit the
cassette as `tests/cassettes/incident-YYYY-MM-DD.jsonl`. Add a layer-4
test that replays it. The QA scenario in caretaker-qa is then a
human-readable echo of the cassette.

## Troubleshooting

### Scheduled run isn't picking up the pin bump

The workflow caches `caretaker` from PyPI / git ref by version. After
bumping `.github/maintainer/.version`, also confirm the workflow's
`uses:` or `pip install` line resolves the new version. If the bump
PR was merged but the next run still shows the old version in
`caretaker --version`, check `actions/cache` keys.

### Self-heal loop fires after a pin bump

The most common cause is a config field that was added in the new
release and is required (or has a different default). Read the
`Config error` issue body — it usually quotes the validation error.
Map that to a `setup-templates/config-default.yml` change in the
release's PR and apply it to caretaker-qa's config.

### Scenarios marked caretaker:qa-passed re-open the next cycle

If caretaker keeps re-opening a closed scenario, the closing comment
probably lacked the `caretaker:qa-passed` label or the
`<!-- caretaker:qa-scenario -->` marker is on a comment instead of
the issue body. Markers are body-only.

## Related skills

- [caretaker-debug](./caretaker-debug.md) — for diagnosing run
  failures surfaced by a QA cycle
- [caretaker-config](./caretaker-config.md) — for tweaking
  caretaker-qa's `.github/maintainer/config.yml` between cycles
- [caretaker-upgrade](./caretaker-upgrade.md) — for the
  honor-the-upgrade-chain alternative in step 2

## References

- `docs/plans/2026-04-25-qa-orchestration-test-plan.md` — full
  layered test strategy
- `docs/qa-findings-2026-04-23.md` — example findings doc from a
  prior cycle
- `docs/plans/2026-04-22-qa-scenario-11-prompt-cache.md` — example
  scenario authoring document
- [caretaker-qa testbed](https://github.com/ianlintner/caretaker-qa)
