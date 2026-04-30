# Orchestrator self-chaining plan

## Goal

Compress multi-cycle PR transitions into a single `_process_pr` invocation by self-chaining internal-only actions, without changing semantics for actions that genuinely wait on external state.

## Today vs. target

**Today** — `_process_pr` is one-shot per cycle:

```
webhook → _process_pr → evaluate → dispatch ONE action → return
                                                        ↑
                                          (next action waits for next cycle)
```

A caretaker-authored PR with green CI takes **at minimum two cycles** to land: cycle 1 dispatches `request_review_approve` → state goes `MERGE_READY` → return. Cycle 2 (next webhook or tick) re-evaluates → dispatches `merge`. Linked-issue cleanup waits for yet another cycle (the bulk triage adapter run).

**Target** — `_process_pr` drives the PR until external wait:

```
webhook → _process_pr → evaluate → dispatch → if internal-only:
                                                  re-evaluate cheaply, dispatch next
                                              if external-wait:
                                                  return
```

That same caretaker PR lands in **one** invocation: approve → merge → cascade → archive ownership comment. Three GitHub round-trips replace what used to be three cycles + three full state re-fetches.

## Self-chaining boundaries

| Action transition | Type | Why |
|---|---|---|
| `request_review_approve` succeeds → `MERGE_READY` → `merge` | **internal** | The auto-approve is our own write; no external state changed. We can patch `evaluation.reviews.approved=True` in place and re-dispatch. |
| `merge` succeeds → `MERGED` → cascade (close linked issues) | **internal** | The merge happened in our process; we know which issues to close. |
| `request_fix` → wait for Copilot push | **external** | Coding agent has to actually push. Yield. |
| `request_review_fix` → wait for push | **external** | Same. |
| `wait` / `wait_for_fix` / CI_PENDING | **external** | Waiting on CI to finish. Yield. |
| `approve_workflows` | **external** | Just toggled action_required runs; CI now needs to run. Yield. |

Triage closure (`CLOSED`) and stuck escalation (`ESCALATED`) already terminate cleanly in the current cycle — no change needed there.

## Design

### Driver shape

Wrap the existing `match evaluation.recommended_action` block in a bounded loop:

```python
hops = 0
while True:
    tracking = await self._dispatch(action, ...)
    if hops >= _MAX_SELF_CHAIN_HOPS:
        break
    next_step = self._next_self_chain_step(prev_action=action, tracking=tracking, report=report, evaluation=evaluation)
    if next_step is None:
        break  # external-wait or terminal
    action, evaluation = next_step  # patched evaluation reflects what we just did
    hops += 1
```

`_next_self_chain_step` returns one of:
- `("merge", patched_evaluation)` — after a successful auto-approve, with `reviews.approved=True` patched in
- `("cascade", evaluation)` — after a successful merge
- `None` — anything else

`_MAX_SELF_CHAIN_HOPS = 3` (approve → merge → cascade is the longest legitimate chain). Hop overflow logs a warning and falls through to ownership archival.

### Cascade integration

`triage_adapter` keeps doing the bulk cascade for PRs the agent didn't merge (human merges, externally-merged drift, replays). The new per-PR `_handle_cascade(pr, tracking, report)` fires only when *we* just merged. It uses `on_pr_merged` and `apply_cascade` from `caretaker.pr_agent.cascade` exactly like the triage adapter does, scoped to the single PR.

`tracked_issues` threads through `PRAgent.run(...)` → `_process_pr(...)` as an optional dict. When `None` (call sites that don't have it handy: tests, single-PR utilities), the cascade hop is skipped — fail-safe.

### What does NOT change

- The state machine in `states.py`. Same verdicts, same recommended_action vocabulary.
- Action handler bodies. `_handle_merge`, `_handle_review_approve`, etc. keep their semantics.
- The triage-adapter bulk cascade. It still runs each tick — covers PRs we didn't directly merge.
- External-wait actions. Same yield behaviour.

### Failure semantics

If a self-chained hop fails (e.g. merge after auto-approve hits a 500), the hop's own error handling fires (`report.errors.append`, `report.waiting.append`) and the chain stops. The earlier hops are not rolled back — auto-approve already landed, that's correct state. Worst case: PR is approved + waiting for next cycle to retry merge, which is exactly what would have happened in the old one-action-per-cycle model.

## Test plan

1. **`test_auto_approve_self_chains_into_merge`** — caretaker PR with green CI: one `_process_pr` call should produce both `report.approved` and `report.merged` entries; `tracking.state == MERGED`.
2. **`test_merge_self_chains_into_cascade`** — caretaker PR with `Closes #123` body: one merge call should close issue #123 in the same cycle.
3. **`test_self_chain_caps_at_max_hops`** — synthetic infinite-loop scenario verifies the hop cap; no runaway, warning logged.
4. **`test_external_wait_action_does_not_chain`** — `request_fix` returns after one hop (Copilot push not synthesisable).
5. **`test_chain_failure_stops_loop_preserves_earlier_hops`** — auto-approve succeeds, merge fails; chain stops, `report.approved` has the entry, `report.errors` has the merge failure.
6. **`test_no_tracked_issues_skips_cascade_hop`** — when `tracked_issues=None`, merge does not chain into cascade (fail-safe for callers without issue state).

## Out of scope

- Generalising self-chaining to other transitions (review-FIX → coding-agent → CI re-run can't chain because the coding agent push is genuinely external).
- Replacing the bulk triage cascade — keep both; bulk handles externally-merged PRs.
- Reordering the action ladder. Same vocabulary, same triggers.
