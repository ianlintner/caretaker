# QA Test Plan — Caretaker Orchestration / PR Lifecycle State Machine

**Status:** Brainstorm / proposed
**Author:** drafted via opencode session 2026-04-25 (post v0.19.3 release)
**Owner:** TBD
**Related work:** `docs/qa-findings-2026-04-23.md`, `docs/plans/2026-04-22-qa-scenario-11-prompt-cache.md`, `docs/review-agent-plan.md`, [caretaker-qa](https://github.com/ianlintner/caretaker-qa)

---

## 1. Why this plan exists

We just shipped two fixes that were extremely hard to detect ahead of time:

- **#581** — orchestrator soft-fail when only transient (rate-limit) errors occurred and `work_landed=False`
- **#582** — `pr_agent._handle_review_approve` idempotency: `last_approved_sha` SHA-tracking, defensive `is_caretaker_pr` guard, default-flip `auto_approve_caretaker_prs` / `close_on_infeasible_review` to `False`

Both bugs ship in code that is **highly stateful, multi-actor, asynchronous, and partially LLM-driven**. Existing unit tests catch isolated branches (mock GitHub, mock LLM, mock state) but they cannot easily exercise:

- The full **PR lifecycle state machine** across realistic event sequences
- **Idempotency under retries**, race conditions, and re-entrancy on the same SHA / different SHA
- **Cross-agent handoffs**: `pr_reviewer` → labels → `pr_agent` → `_handle_review_*` → external Copilot/Claude review fix → re-review → approve → merge
- **Multi-actor interleavings**: caretaker's own actions vs. webhook re-deliveries vs. concurrent dispatcher runs vs. Copilot's reactions

The aim of this plan is to design a **layered QA test strategy** so that the orchestration logic — not just the unit-level pieces — is exercised. We want to catch the next "transient-only failure flagged the run" or "approve-loop on the same SHA" class of bug **before it ships**.

---

## 2. Goals (what "done" looks like)

1. **State-machine-level confidence**: any state transition in `pr_agent.states` and `orchestrator` has at least one direct test that drives it from a serialized event log.
2. **Idempotency contract**: every external write the orchestrator makes (GitHub create_review, create_comment, update_issue, label add/remove, dispatch) is covered by a test that replays the same event twice and asserts the second run is a no-op (or the only difference is reading state).
3. **Adversarial event sequences**: a property-based / fuzz harness can generate plausibly-bad webhook orderings (review BEFORE PR open, synchronize during fix-loop, two reviews seconds apart, rate-limit mid-handler, app gets re-installed mid-run) and the orchestrator never:
   - double-approves the same SHA
   - loses a tracked PR from state
   - posts a duplicate "fix me" comment without dedup
   - exits with code 1 when only transient errors occurred
4. **Live-fire QA in caretaker-qa**: the existing `caretaker-qa` security-relevance agent repo gets purpose-built scenarios (synthetic PRs, synthetic issues) that drive caretaker through every action of the lifecycle on a real repo with real webhooks, and a scoreboard of those runs is published per-release.
5. **Skill / runbook**: contributors and the caretaker agent itself can pull a single skill describing "how to design a state-machine test for caretaker" and follow it without reinventing the methodology each time.

---

## 3. Non-goals

- We are **not** trying to test the underlying LLM (Claude / GPT-5) — only the harness, prompts, and our reaction to its outputs. LLM model behavior is covered by eval harnesses elsewhere.
- We are **not** trying to test GitHub itself. We treat GitHub as a contract surface (GraphQL + REST + webhook payloads) and either replay it or use a fake.
- We are **not** trying to replace `tests/test_pr_agent/` unit tests. This plan adds *layers above* them.

---

## 4. Layered test strategy (the brainstorm)

Five layers, each with a different cost/coverage tradeoff. Tests should be checked in at the lowest layer that can plausibly catch the bug — the higher layers exist to catch what the lower ones can't.

### 4.1 Layer 1 — Pure state-machine unit tests (already partially exist)

**Where:** `tests/test_pr_agent/test_states.py` and `tests/test_pr_agent/test_agent.py`.
**What's missing today:** state transitions are tested mostly in isolation. We don't have a single "given this event log, the final TrackedPR + recommended_action looks like X" test.

**Proposed additions:**

- `test_state_machine_table.py`: a parametrized table of `(prior_state, event, expected_recommended_action, expected_next_state)` covering **every cell** of the matrix, with a `pytest.param(id=...)` per cell so failures point at the offending transition. This is the same approach as Linux kernel netlink tests.
- `test_idempotency_table.py`: for each handler (`_handle_review_approve`, `_handle_review_close`, `_handle_review_fix`, `_handle_merge_ready`, `_handle_request_changes_received`, etc.) replay the same event twice and assert side effects on the second run are empty (no GitHub write, no state delta, no report-line append).

**Pattern:**

```python
@pytest.mark.parametrize("prior_state, event, expected_action", [
    pytest.param(PRState.UNKNOWN,        Event.PR_OPENED,        "request_review", id="open->review"),
    pytest.param(PRState.CI_PASSING,     Event.REVIEW_APPROVED,  "merge",          id="approve->merge"),
    pytest.param(PRState.MERGE_READY,    Event.REVIEW_APPROVED,  "noop",           id="approve-on-merge-ready-noop"),
    pytest.param(PRState.FIX_REQUESTED,  Event.SYNCHRONIZE,      "wait_for_ci",    id="sync-after-fix"),
    # ... one row per cell ...
])
def test_state_machine_transition(prior_state, event, expected_action): ...
```

### 4.2 Layer 2 — Event-log replay (golden tests)

**Where:** new `tests/test_pr_agent/test_event_log_replay/` directory.
**Pattern:** record a **canonical event log** (a list of `(timestamp, event_type, payload_dict)` tuples) for each of \~12 named scenarios. Replay through a `FakeGitHub` (already present in `tests/test_pr_agent/conftest.py`) that captures every API call. Assert against a **golden** snapshot of `(state.json, github_calls.jsonl, report.json)`.

**Named scenarios (initial set):**

| # | Name | Tests |
|---|------|-------|
| 1 | `happy_path_caretaker_pr` | open → CI green → review (no findings) → approve → merge |
| 2 | `caretaker_pr_with_fix_loop` | open → CI red → review → fix → CI green → re-review → approve |
| 3 | `infeasible_review_close` | open → CI green → review says "duplicate of #X" → CLOSE verdict → close PR with single-line reason |
| 4 | `escalate_high_loc_blocker` | open(800 LoC) → CI green → review with blocker → ESCALATE not CLOSE |
| 5 | `transient_rate_limit_only` | orchestrator hits 403 → no real work → exit 0 (regression test for #581) |
| 6 | `repeat_approve_same_sha` | force two approve runs with same head_sha → second run is no-op (regression test for #582) |
| 7 | `new_sha_re_approves` | first approve at sha=A → push sha=B → second approve runs |
| 8 | `non_caretaker_pr_skipped` | dependabot/foo branch → never auto-approved even with all green |
| 9 | `webhook_double_delivery` | identical webhook delivered twice 2s apart → no double-write |
| 10 | `concurrent_runs` | run N=2 in parallel on same tracking issue → no clobber, no duplicate review |
| 11 | `review_arrives_before_ci` | review submitted while CI still running → wait, do not approve until CI green |
| 12 | `app_reinstall_mid_run` | tracking issue exists, app token rotates → run completes successfully |

Each scenario has a YAML or JSON event log that humans can read and edit:

```yaml
scenario: happy_path_caretaker_pr
events:
  - { t: 0,   type: pull_request,        action: opened,      head_ref: caretaker/foo, head_sha: aaa }
  - { t: 60,  type: check_suite,         action: completed,   conclusion: success,     head_sha: aaa }
  - { t: 120, type: pull_request_review, action: submitted,   state: commented,        body: "LGTM" }
expected:
  github_calls:
    - { method: POST, path: /repos/.../reviews, body.event: APPROVE }
    - { method: PUT,  path: /repos/.../merge }
  tracking_state: MERGE_READY
  report.errors: []
  exit_code: 0
```

### 4.3 Layer 3 — Property-based / fuzz layer

**Where:** new `tests/test_pr_agent/test_event_fuzz.py`.
**Tool:** `hypothesis` (already a transitive dep via `pytest`-style ecosystem; if not, add it).

**Strategy:** define a `Hypothesis` strategy over event sequences:

```python
event = st.one_of(
    st.builds(PROpened, head_ref=branches, head_sha=shas),
    st.builds(CheckSuite, conclusion=st.sampled_from(["success", "failure", "neutral"]), head_sha=shas),
    st.builds(PRReview, state=st.sampled_from(["commented", "approved", "changes_requested"]), body=review_bodies),
    st.builds(RateLimitInjection, after_n_calls=st.integers(0, 10)),
)
event_log = st.lists(event, min_size=1, max_size=20)

@given(log=event_log)
@settings(max_examples=200, deadline=timedelta(seconds=5))
def test_orchestrator_invariants(log):
    state, calls, report = await replay(log)
    # Invariants that must hold for ANY event log:
    assert no_double_approve_same_sha(calls)
    assert no_duplicate_close_comment(calls)
    assert exit_code == 0 if all(e.kind in {"transient"} for e in report.errors) else 1
    assert tracking.last_approved_sha is None or tracking.last_approved_sha in seen_shas(log)
    assert state.tracked_prs is not None  # never lost
```

**What this catches that 4.1/4.2 miss:** unexpected event orderings (e.g. `merge` arriving before `approve`, two `synchronize` events back-to-back during fix-loop, `closed` arriving between `approve` and `merge`).

### 4.4 Layer 4 — Recorded-cassette integration (existing pattern, expand)

**Where:** `tests/test_orchestrator/` already uses recorded fixtures. Expand.
**What's new:** a `tests/cassettes/` directory of real captured webhook + GitHub API exchanges from caretaker-qa runs. Use `vcrpy` or a hand-rolled jsonl format. Replay them with the **current** orchestrator and snapshot the diff of state + outbound calls.

**Why:** any contract drift between our `FakeGitHub` and real GitHub gets caught here. Also a great regression vault — when we ship a fix, the cassette of the bug-causing run becomes a permanent test.

**Capture loop:** add an opt-in env var `CARETAKER_RECORD_CASSETTE=path` that, when set, dumps every outbound GitHub call + every webhook payload to a jsonl. caretaker-qa runs in nightly mode with this on, the resulting cassettes are committed to caretaker as test fixtures.

### 4.5 Layer 5 — caretaker-qa as a live-fire scoreboard

**Where:** caretaker-qa already exists. Each maintainer run there is a real exercise. Today there is **no scoreboard or assertion** — it's just "the run succeeded."

**Proposed:**

1. Add a `scenarios/` directory in caretaker-qa containing pre-fab scripts that **deterministically produce** specific PR/issue patterns (a tiny chaos-engineering library): "open a PR with `caretaker/x` head, push 3 commits, post a review saying duplicate, push another commit", etc.
2. Add a per-release **smoke matrix** GitHub Action in caretaker-qa: on `repository_dispatch` with the new caretaker version pinned, run all scenarios serially, then generate a `qa-report.md` with one row per scenario showing whether caretaker took the right end-to-end action.
3. Publish that report as a release-artifact on caretaker (gate the next release on it).

This is the only layer that exercises the **real LLM**, real GitHub permissions, real webhook timing, real concurrency. Slow (\~30min per release) but extremely high-signal.

---

## 5. Tooling proposal: a "caretaker-state-machine-test" skill

There's an obvious meta-improvement. Right now the methodology lives in the head of whoever is debugging — that's how we ended up with #581 and #582 *after* incidents. Capture it as a [skill](https://github.com/anthropics/skills) so:

- The caretaker agent itself can load it during PR review and check "did this PR add a state transition? then it should also add a row in `test_state_machine_table.py`".
- New contributors get the methodology for free.

**Skill name:** `caretaker-state-machine-test`
**Trigger phrases:** "test state machine", "test orchestration flow", "caretaker QA", "PR lifecycle test", "test idempotency"
**Skill content (sketch):**

```
SKILL.md
- when to use: any PR touching pr_agent/states.py, pr_agent/agent.py _handle_*, orchestrator.py exit-gate, pr_reviewer/agent.py
- step 1: identify which layer (4.1-4.5) is appropriate
- step 2: open tests/test_pr_agent/test_state_machine_table.py and add a row
- step 3: if a new external GitHub call was added, also add an idempotency-table row
- step 4: if a new event TYPE was added, extend the Hypothesis strategy in test_event_fuzz.py
- step 5: if behavior is observable end-to-end on a real repo, add a scenarios/ entry in caretaker-qa
- references: this plan, docs/review-agent-plan.md, tests/test_pr_agent/conftest.py FakeGitHub
- script: a one-shot generator that prints a stub test from the diff of pr_agent/agent.py
```

Bundle a script `skills/caretaker-state-machine-test/scripts/stub-from-diff.py` that takes `git diff main -- src/caretaker/pr_agent/` and emits skeleton parametrized tests for each new branch in `_handle_*`.

---

## 6. BDD-style "executable specifications" (optional layer 1.5)

For the highest-traffic flows (caretaker PR auto-approve, infeasible-review close, fix-loop), Gherkin-style specs **read like the user-story** and protect against regressions in semantics — not just code paths.

```gherkin
Feature: Caretaker auto-approves its own clean PRs
  Scenario: A new caretaker PR with green CI and a non-blocking review
    Given a tracked PR on branch caretaker/foo at sha "aaa"
    And  CI status is success
    And  one review is submitted with state "commented" and body "LGTM"
    And  ReviewConfig.auto_approve_caretaker_prs is True
    When the orchestrator runs
    Then the PR is approved exactly once at sha "aaa"
    And  the tracking state is MERGE_READY
    And  TrackedPR.last_approved_sha equals "aaa"

  Scenario: Same PR, second orchestrator pass on same sha
    Given the previous scenario completed
    And  TrackedPR.last_approved_sha equals "aaa"
    When the orchestrator runs again with no new events
    Then no review is submitted
    And  the tracking state remains MERGE_READY
```

Use `pytest-bdd` (lightweight, no separate runner) so the specs live next to the unit tests. Each `Then` clause maps to an assertion against the same `FakeGitHub` + state used in 4.2.

**Value:** when a non-Python contributor (or the caretaker agent in shadow mode) wants to know "what does caretaker promise to do in this scenario?" the answer is `*.feature` files, not 1500 lines of Python.

---

## 7. Concrete next steps (sequenced; address after current QA validation work completes)

1. **Land the bookkeeping** (low cost, high leverage):
   - [ ] Promote this file from `docs/plans/` to `docs/testing/orchestration-qa.md` once the methodology stabilizes
   - [ ] Add a tracking issue with `meta:qa` label that links here

2. **Layer 4.1 — table tests** (1 PR, ~half-day):
   - [ ] Audit existing transitions in `pr_agent/states.py`; build the `test_state_machine_table.py` parametrize table
   - [ ] Build the `test_idempotency_table.py` for every `_handle_*` (extends what we already added in #582)

3. **Layer 4.2 — golden replay** (1 PR, ~1 day):
   - [ ] Build `replay()` helper on top of `FakeGitHub`
   - [ ] Author the 12 named scenarios (likely ~150 LoC of YAML each)
   - [ ] Wire snapshot assertions (use `syrupy` or hand-rolled `pytest_diff`)

4. **Layer 4.3 — fuzz** (1 PR, ~half-day after 4.2):
   - [ ] Add `hypothesis` to dev deps
   - [ ] Define event strategies and invariants
   - [ ] Run on CI with `--hypothesis-profile=ci` (200 examples) and nightly with `--hypothesis-profile=nightly` (5000 examples)

5. **Layer 4.4 — cassettes** (1 PR, ~half-day):
   - [ ] Add the `CARETAKER_RECORD_CASSETTE` env var to orchestrator + GitHubClient
   - [ ] Run caretaker-qa once with it on, commit the resulting cassette
   - [ ] Add `tests/test_orchestrator/test_cassette_replay.py`

6. **Layer 4.5 — caretaker-qa scoreboard** (1 PR each repo, ~1 day):
   - [ ] Add `scenarios/` skeleton + `make_pr.sh` / `make_issue.sh` helpers in caretaker-qa
   - [ ] Add `qa-smoke-matrix.yml` workflow gated by `repository_dispatch`
   - [ ] Modify caretaker `release-publish.yml` to dispatch the smoke matrix and wait

7. **Skill + BDD** (optional, ~half-day each):
   - [ ] `caretaker-state-machine-test` skill in `skills/`
   - [ ] `pytest-bdd` for the top-3 user-facing flows in section 6

**Total estimated effort: ~3-5 days of focused work for layers 4.1-4.4, +~1 day each for the optional layers.**

---

## 8. Risks / open questions

- **Hypothesis flakiness on CI**: timing-sensitive invariants need careful handling. Mitigate with `deadline=None` for the orchestrator-level shrink-replay.
- **Cassette drift**: GitHub API contract changes will break replay. Mitigate by recording at the `aiohttp` raw-bytes layer and asserting on parsed diffs, not byte equality.
- **caretaker-qa cost**: each scenario costs Azure AI / Claude tokens. Mitigate by only running the smoke matrix on release tags, not every push.
- **Concurrency tests are hard**: 4.1-style table tests can't easily express "two runs racing." We rely on 4.3 fuzz + 4.5 live-fire to cover this. Consider a `pytest-asyncio` based `asyncio.gather(run_a, run_b)` harness with synthetic `await` points.
- **The state machine is implicit**: today, transitions are scattered across `states.py` + `agent.py`. Refactoring to a single explicit `transition(state, event) -> (state, action)` table would make 4.1 trivial. Tracking as a separate refactor.

---

## 9. Appendix — invariants we must protect

These are the **always-true** properties any caretaker run must preserve. They're the assertions in 4.3, the post-conditions in 4.6, and the smoke-test pass criteria in 4.5.

1. **No double-approve same SHA** — `count(create_review event=APPROVE for pr=N at sha=S) <= 1`
2. **Caretaker PRs only auto-approve** — `pr.head_ref starts with caretaker/ or claude/ or copilot/` is required for any APPROVE
3. **Transient-only errors do not fail the run** — if `report.errors` only contains `kind in {RATE_LIMIT, TRANSIENT_NETWORK}`, then `exit_code == 0`
4. **Tracking state monotonicity** — `MERGE_READY` and `CLOSED` are sinks; we never observe them transitioning back into earlier states without an explicit user action
5. **No duplicate close-comment** — `count(create_comment with marker=<!-- caretaker:review-close -->) <= 1` per PR
6. **No duplicate fix-loop comment** — `_has_pending_task_comment(pr) == True ⇒ no new copilot_bridge.request_review_fix call`
7. **Sanitized reasons** — every `_handle_review_close` body has no embedded newline in the blockquote line
8. **State persistence is best-effort** — if `state_save_skipped` due to rate-limit, run still exits cleanly with a warning, not error
9. **Escalation is opt-in** — `ESCALATE` verdict requires either explicit signal in review body OR (`severity==blocker` AND `pr_additions > high_loc_threshold`)
10. **App identity respected** — caretaker never approves a PR authored by itself unless `is_caretaker_pr` is True (i.e. branch-name predicates match)

---

*End of plan.*
