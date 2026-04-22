# Caretaker Source Audit — Engineering Plan of Record (2026-04-22)

## 1. Top-line health read

Caretaker is in the architecturally-ambitious-but-drifting bucket. Twelve minor versions in ~3 weeks, ~40 subsystems, a memory-graph milestone plan that shipped M1–M8 in a single week, a fleet-registry with OAuth2 and Neo4j projection, and at the same time PR #418, #462, #412, #413 show caretaker still failing its own CI on `main` and filing self-heal issues about "unknown" failures. The central anti-pattern is exactly what the user flagged: brittle keyword-and-regex judgement embedded in `pr_agent`, `issue_agent`, `pr_reviewer`, `ci_triage`, `review`, `evolution/crystallizer`, and the webhook dispatch guard — all places where a short LLM call with a structured schema would be far more robust and cheaper to maintain. The Anthropic SDK call path (`llm/provider.py:158`) does not use prompt caching despite caretaker's repeated large-context calls to the same system prompt, which is leaving real money on the table. The PR-agent main file (`pr_agent/agent.py`, 1022 LOC) is on the verge of "god module." There is a lot of sound engineering here — bitemporal graph edges, fail-open fleet emitter, rate-limit cooldowns, idempotent comment upsert, the dispatcher/executor split — but the decision layer that sits between GitHub state and those mechanisms is mostly if/elif ladders that should be model-synthesised.

## 2. Critical bugs (fix now)

1. `src/caretaker/pr_agent/agent.py:178, 223, 408, 584` and peers — ten call sites use `datetime.utcnow()` which is deprecated in 3.12 and, more importantly, produces naive datetimes that get mixed with tz-aware datetimes elsewhere (`last_copilot_attempt_at` uses `datetime.now(UTC)`). The `_as_utc` helper in `orchestrator.py:51` already exists to paper over this; prior PR #300 had to fix a similar bug. Fix: replace all `utcnow()` with `datetime.now(UTC)` and strip `_as_utc` defensive wraps.

2. `src/caretaker/pr_agent/agent.py:110-132` — `pr_number` is used as the function parameter name and then reused as the loop variable on line 150 (`for pr_number, tracked in list(tracked_prs.items())`). The outer `pr_number` parameter is shadowed for the rest of the function body. This is benign today but a footgun for anyone editing the function. Rename the loop var to `tracked_pr_number`.

3. `src/caretaker/pr_agent/states.py:367` — When reviews are `pending` (no reviewers at all) and CI is green, caretaker recommends `merge` unconditionally. `evaluate_merge` then separately checks `config.auto_merge.human_prs`, but the recommended_state is `MERGE_READY`, so a human PR with no reviewers and green CI will report `ready for merge` in the status comment even if auto-merge is disabled. Split `MERGE_READY` from `AWAITING_REVIEW` based on the same auto-merge config.

4. `src/caretaker/pr_agent/pr_triage.py:150-167` — "duplicate of" selector picks `max(prs, key=created_at)` — the newest PR — and closes the older siblings. The docstring for `close_duplicate_issues` in the twin `issue_triage.py` says the opposite: "Survivor: oldest issue by created_at (preserves the canonical history)." Two close cousins with opposite rules is almost certainly a bug. Pick one, match docs.

5. `src/caretaker/foundry/tool_loop.py:106-125` — the "defensive" fallback when `raw_message` is `None` builds an assistant message with `tool_calls` in OpenAI function-calling shape, but for Anthropic tool-use the required shape is different (`content` blocks with `type: tool_use`). If any LiteLLM path ever returns `raw_message=None` for an Anthropic model the next turn will 400. Fix: drop the fallback and require providers to always populate `raw_message`, or branch on provider family.

6. `src/caretaker/fleet/emitter.py:58-97` — `_oauth_client` is a **module-level** mutable global. If two `MaintainerConfig`s with different OAuth creds run in the same process (tests, the admin backend's multi-tenant path) the second build silently mutates the first's cache. Fix: move the cache onto a thin `FleetOAuthClientCache` instance owned by the `Orchestrator`.

7. `src/caretaker/pr_agent/agent.py:450-459` — `_has_pending_task_comment` iterates comments twice and implicitly assumes they're sorted by id ascending. `get_pr_comments` does not guarantee order; if the API ever returns them reversed, the "last task before any result" logic inverts. Sort by `(created_at, id)` explicitly.

## 3. Existing-feature improvements (agentic migration candidates)

1. **PR readiness scoring** (`src/caretaker/pr_agent/states.py:178-259`). Today a fixed-weight (10/20/30/40) additive rubric produces a score and blocker list. This is exactly the "is this PR ready?" judgement the user wants model-driven. Replace with an LLM call that sees the PR payload, check-run summaries, review bodies, labels, linked issues, and returns:

   ```python
   class Readiness(BaseModel):
       verdict: Literal["ready", "blocked", "pending", "needs_human"]
       confidence: float  # 0..1
       blockers: list[Blocker]  # each with category + human_reason + suggested_action
       summary: str  # <= 200 chars for the status comment
   ```
   Keep the imperative path as a fallback when `llm.available` is False. The caretaker/pr-readiness check-run body then comes from the model's `summary` + structured blocker list rather than concatenated blocker tokens.

2. **Webhook dispatch guard — "is this caretaker's own echo?"** (`.github/workflows/maintainer.yml:104-140` JS block and mirrored actor set in `src/caretaker/pr_agent/_constants.py:7-15`). Two hand-maintained bot-login sets, one regex, one "explicit command" string match. The model is a natural fit. Move the logic inline into caretaker: on `issue_comment`, call an LLM with the comment body + actor + recent action history and get `{is_self_echo: bool, is_human_intent: bool, suggested_agent: str | null}`. Keep the YAML guard as a cheap "probably-self" prefilter for cost control.

3. **CI failure triage** (`src/caretaker/pr_agent/ci_triage.py:38-128`). Keyword regex ladder across TEST/LINT/BUILD/TYPE/TIMEOUT/BACKLOG/UNKNOWN. The LLM is already invoked for log summarisation inside `analyze_ci_logs`, but classification is still regex. Ironically, classify_failure sees mostly `UNKNOWN` on the `main` CI failures (issues #412/#413/#462). Structured output:

   ```python
   class FailureTriage(BaseModel):
       category: Literal["test", "lint", "build", "type", "timeout", "flaky", "backpressure", "infra", "unknown"]
       confidence: float
       is_transient: bool  # caller uses this to decide retry vs fix
       root_cause_hypothesis: str
       minimal_reproduction: str | None
       suggested_fix: str
       files_to_touch: list[str]
   ```
   `is_transient` replaces today's `NON_ACTIONABLE_CONCLUSIONS` frozenset and the `flaky_retries` integer counter.

4. **Review-comment classification** (`src/caretaker/pr_agent/review.py:36-86`). The keyword list (`nit:`, `bug`, `must`, `?`) is embarrassing for a "principal-engineer-quality" review. The LLM path exists (`analyze_review_comment`) but falls back to heuristics and returns a free-text blob that isn't parsed. Require structured output, delete `classify_review_basic`:

   ```python
   class ReviewClassification(BaseModel):
       kind: Literal["actionable", "nitpick", "question", "praise", "discussion"]
       severity: Literal["blocker", "major", "minor", "trivial"]
       summary_one_line: str
       requires_code_change: bool
       suggested_prompt_to_copilot: str | None
   ```

5. **Issue duplication + classification** (`src/caretaker/issue_agent/classifier.py:30-127` and `src/caretaker/issue_agent/issue_triage.py:48-61`). Title-hash + CVE regex for duplicate detection, plus a huge keyword ladder for BUG vs FEATURE vs QUESTION. Replace with a single issue-triage LLM call that sees the issue + embedding-similar recent issues and returns kind, suggested labels, duplicate-of candidate with confidence, and stalestate. Keep the CVE regex as a deterministic pre-filter (regex is genuinely right for structured CVE IDs).

6. **Cascade close-linked-issues / redirection logic** (`src/caretaker/pr_agent/cascade.py:132-184`). "PR body <200 chars AND single linked issue → close PR" is the kind of heuristic that will mis-fire on a one-line diff with "Fixes #N". Let the model inspect the PR + the canonical issue and return `{action: "redirect"|"close"|"keep_open", justification: str}`.

7. **"Is this comment from the bot?" detection** (repeated in `pr_agent/_constants.py`, `pr_agent/agent.py:214`, `foundry/executor.py`, `.github/workflows/maintainer.yml`). Five different places string-match `[bot]`, `copilot`, `the-care-taker[bot]`, etc. Push the check into one helper that asks an LLM once per unfamiliar login and caches the verdict in `memory/core.py` keyed on login; fall back to the current allowlist when caching is disabled.

8. **Stuck-PR escalation** (`src/caretaker/pr_agent/agent.py:196-216, 230-253`). `stuck_age_hours` and `is_pr_stuck_by_age` are tight thresholds that don't reflect the actual state: a human-reviewed PR stuck at "waiting for release" for 3 days is not the same as a Copilot PR stuck in CI failure. Let the LLM judge:

   ```python
   class StuckVerdict(BaseModel):
       is_stuck: bool
       stuck_reason: Literal["abandoned", "awaiting_human_decision", "ci_deadlock", "merge_queue", "not_stuck"]
       recommended_action: Literal["escalate", "nudge_reviewer", "request_fix", "wait", "close_stale"]
       explanation: str
   ```

9. **Executor routing (size classifier + pre/post-flight)** (`src/caretaker/foundry/size_classifier.py`, `pr_reviewer/routing.py`). Both are fine-grained point systems (LOC + files + sensitive-path regex) that the user's guidance says are ambiguous on the happy path. Have the model return `{path: "inline" | "foundry" | "claude_code" | "copilot", reason: str, risk_tags: list[str]}`. The small cost is acceptable because routing happens once per PR.

10. **Failure-crystallization category inference** (`src/caretaker/evolution/crystallizer.py:25-39`). Category-by-regex. Swap for the same triage call that classifies CI failures (item 3): you get a category for free and stop maintaining two regex tables.

## 4. Enhancements (net-new but scoped)

1. **Anthropic prompt caching across the system prompt prefix**. `llm/provider.py:158-178` sends the full `messages` list every call. Caretaker's `FoundryExecutor` in particular sends the ~1KB `_BASE_SYSTEM_PROMPT` + denylist + allowed_commands + task_guidance on every tool-loop iteration. Add `cache_control: {type: "ephemeral"}` on the system block and on stable user-side skills hints; measure cache-hit rate via the `usage.cache_read_input_tokens` / `cache_creation_input_tokens` fields. Acceptance: ≥50% cache-read ratio on tool-loop iterations 2..N; emitted as a `llm_cache_hit_ratio` Prometheus counter.

2. **Cross-run memory retrieval for the PR agent**. `memory/core.py` publishes an `AgentCoreMemory` node on every dispatch but nothing reads it. On PR entry, retrieve the 3-5 most-similar past PR core-memory snapshots (cosine over embedded summary) and inject them into the readiness LLM call. Leverages data already captured. Shape: new `caretaker/memory/retriever.py` with `find_relevant(agent, pr_context) -> list[CoreMemoryHit]`.

3. **Skill promotion loop wiring**. `fleet/graph.py` projects `:Skill` nodes into `:GlobalSkill` when signature appears in ≥ min_repos. There is no path from `:GlobalSkill` back into `build_prompt` / `_format_skills` — fleet promotion is write-only. Wire `InsightStore.get_relevant` to union local skills with GlobalSkill candidates (cheap Neo4j query) so a new client repo gets day-one benefit from the fleet.

4. **Fleet admin dashboard: exceptions & alerts**. Fleet heartbeats have counters + `goal_health` but the admin UI only renders them. Add a thin alerting path: when a repo's `goal_health` falls below threshold for N consecutive heartbeats or `error_count` spikes, emit a `FleetAlert` node in the graph and surface in the Fleet tab. New file: `caretaker/fleet/alerts.py`; reuses `run_history` already in state.

5. **Dry-run "shadow" mode for agentic migrations**. Before flipping any of §3's LLM handovers to authoritative, run the LLM in parallel with the existing heuristic, log disagreements into a new `:ShadowDecision` graph node with outcome, and expose a diff report in the admin UI. Makes the migration measurable instead of a leap of faith.

6. **Structured-output validation wrapper**. `pr_reviewer/inline_reviewer.py:112-117` does a raw `json.loads` and degrades silently to `verdict=COMMENT` on any parse error. Introduce a `structured_complete[T: BaseModel](prompt, schema) -> T | None` helper on `ClaudeClient` that: prefixes the schema to the system prompt; attempts pydantic parse; on failure, re-asks the model once with the validation error included ("your previous response failed: <err>, return only valid JSON"). Foundational for every item in §3.

## 5. Research spikes

1. **GitHub App vs scheduled workflow as the primary runtime** (`github_app/` is scaffolded but the production mode is still the maintainer.yml schedule). Why it matters: the dispatch guard, cooldowns, and two-comment dedup all exist because scheduled invocations race. A webhook-driven App would remove that class. 2 days: prototype receiving `pull_request` + `check_run` + `issue_comment`, measure end-to-end latency and race windows vs current schedule.

2. **Prompt-cache TTL and stickiness on Foundry's tool-loop**. Anthropic ephemeral cache is 5 min; tool-loop runs often exceed that. Investigation: measure mean loop duration, test if breaking work into shorter tool-bursts per iteration boost net cache hit rate. 1 day: instrument `tool_loop.py`, run synthetic 20-iteration tasks, graph cache-read tokens per iteration.

3. **Persistence model for cross-repo skill promotion**. Today `:GlobalSkill` lives in Neo4j keyed on the raw signature string; skill *SOP text* is fed through `abstract_sop` in Python. The right long-term store could be a vector index (signature embedding → SOP) for semantic match, not string equality. 2 days: evaluate 3 stores (pgvector, Neo4j vector index, Qdrant) against a corpus of ~500 real caretaker skill rows; measure recall@5 on held-out paraphrased signatures.

4. **Replace the PR state machine enum with a model-emitted state**. `PRTrackingState` has 9 values, `recommended_action` strings are matched in a `match/case` on `pr_agent/agent.py:301-317`. Does the model-first approach render the FSM obsolete entirely, or is the FSM still the audit artefact? 1 day: design doc + small prototype calling Claude with "here is the PR payload + current state, what's the next action" and compare trace quality vs current logs.

5. **OTel GenAI span → CausalEvent → reflection prompt round-trip**. M8 shipped OTel spans and `CausalEvent` now carries `span_id`. Are the reflection prompts actually improved by this richer provenance? 1 day: run the reflection engine on last 30 stuck goals, with and without span-derived context, and ask the model which one produces more specific recommendations.

## 6. Phased rollout plan

**Phase 1 — Foundations (weeks 1–2).** Goal: unblock all later LLM-handover work behind a safe, measured migration path. Workstreams: (W1) fix §2 correctness bugs as separate small PRs; (W2) ship §4.1 prompt-caching on both `AnthropicProvider` and `LiteLLMProvider` with cache-hit metric; (W3) ship §4.6 `structured_complete[T]` helper and migrate `pr_reviewer/inline_reviewer.py` to it as the canary; (W4) ship §4.5 shadow-decision graph node. Exit criteria: all §2 PRs merged, cache-hit ratio visible in Prometheus, one endpoint (`pr_inline_review`) uses pydantic-validated structured output end-to-end. Risk: low; blast radius contained to one reviewer agent. Parallelism: W1/W2/W3/W4 fully independent.

**Phase 2 — Decision migrations (weeks 3–5).** Goal: move the brittle judgement calls from §3 onto the LLM, shadow-first. Workstreams: (W5) PR readiness — §3.1; (W6) CI failure triage — §3.3 + §3.10 (single shared classifier); (W7) review-comment classification — §3.4; (W8) issue triage & dup detection — §3.5; (W9) cascade & stuck-PR judgement — §3.6 + §3.8; (W10) bot-identity consolidation — §3.7. Each lands behind a config flag (`agentic.<domain> = shadow | enforce`). Exit criteria: ≥1 week of shadow data per workstream, disagreement rate <5% before flipping to enforce. Risk: medium; the readiness and triage migrations touch hot paths, shadow mitigates.

**Phase 3 — Leverage what's captured (week 6).** Goal: turn the memory-graph and fleet graph from write-only into read-and-decide. Workstreams: (W11) §4.2 cross-run memory retrieval wired into readiness LLM calls; (W12) §4.3 fleet skill promotion → prompt injection; (W13) §4.4 fleet alerts surface in admin UI; (W14) §3.2 dispatch-guard migration (depends on W10). Exit criteria: at least one merged PR attributable to a retrieved past-skill hint; fleet alert demoable. Risk: low-medium; read-path only for W11/W12.

Research spikes §5.1, §5.2, §5.3 run alongside Phase 1–2 as 1–2-day timeboxes; §5.4 and §5.5 are Phase 3 inputs.

## 7. Parallelization map

| Item | Workstream | Preconditions | Sub-agent prompt seed |
|---|---|---|---|
| §2.1 utcnow | W1 | none | "Replace every `datetime.utcnow()` in `src/caretaker/` with `datetime.now(UTC)` and remove now-redundant `_as_utc` wraps; preserve serialized tz-naive compatibility in pydantic state models." |
| §2.2 pr_number shadowing | W1 | none | "Rename the loop variable in `pr_agent/agent.py` `_terminal` block so the outer `pr_number` parameter stays bound." |
| §2.3 MERGE_READY split | W1 | W1.1 done | "Split PR states so `MERGE_READY` only fires when auto-merge config permits; status comment must never claim 'ready for merge' for a disabled auto-merge profile." |
| §2.4 triage survivor | W1 | none | "Align `pr_triage.close_duplicate_fix_prs` survivor selection with `issue_triage.close_duplicate_issues` (oldest wins); update tests." |
| §2.5 tool_loop fallback | W1 | none | "Remove the OpenAI-shaped fallback in `foundry/tool_loop.py`; require providers to always populate `raw_message` and add a unit test." |
| §2.6 fleet cache | W1 | none | "Replace the module-global OAuth client cache in `fleet/emitter.py` with an instance on `Orchestrator` and thread it through `emit_heartbeat`." |
| §2.7 comment ordering | W1 | none | "Sort comments by `(created_at, id)` inside `_has_pending_task_comment` and add a regression test for reverse-order input." |
| §3.1 readiness LLM | W5 | W3 helper | "Introduce `Readiness` pydantic model, route PR readiness through `structured_complete`, gate behind `agentic.readiness` flag with shadow logging." |
| §3.2 dispatch self-echo | W14 | W10 | "Move the marker+actor loop guard from `maintainer.yml` into an LLM-backed `is_self_echo` detector with a 2-second timeout fallback to the regex." |
| §3.3 CI triage | W6 | W3 | "Rewrite `ci_triage.classify_failure` as a structured LLM call returning `FailureTriage`; delete `_PATTERNS` regexes and `NON_ACTIONABLE_CONCLUSIONS` once shadow data confirms parity." |
| §3.4 review comments | W7 | W3 | "Swap `classify_review_basic` for a `ReviewClassification` structured LLM call; update `analyze_reviews` to propagate severity downstream." |
| §3.5 issue triage | W8 | W3 | "Replace `classify_issue` keyword ladder with a structured LLM call; keep the CVE regex as a pre-filter and add embedding-similarity for dup detection." |
| §3.6 cascade | W9 | W3 | "Route `on_issue_closed_as_duplicate` decisions through the model; preserve deterministic `parse_linked_issues` for the edge lookup." |
| §3.7 bot detection | W10 | none | "Consolidate all `[bot]`/`copilot` string checks behind `caretaker.identity.is_automated(login)`; back it with a memoized LLM lookup when login is unfamiliar." |
| §3.8 stuck-PR | W9 | W3, §3.1 | "Replace `_is_pr_stuck_by_age` threshold with a `StuckVerdict` LLM call; keep `stuck_age_hours` as minimum-age prefilter." |
| §3.9 executor routing | W6/W7 | W3 | "Replace `pr_reviewer/routing.py` + `foundry/size_classifier.py` point systems with a structured routing LLM call." |
| §3.10 crystallizer category | W6 | §3.3 | "Call the shared triage classifier inside `crystallizer._infer_category` and remove `_CATEGORY_PATTERNS`." |
| §4.1 prompt caching | W2 | none | "Add `cache_control: ephemeral` to the system block in both `AnthropicProvider.complete` and `LiteLLMProvider.complete_with_tools`; emit `llm_cache_hit_ratio`." |
| §4.2 memory retrieval | W11 | W5 | "Build `memory/retriever.py`; wire retrieved core-memory snapshots into the readiness prompt under a ≤500-token budget." |
| §4.3 skill promotion | W12 | none | "Union local Skills with :GlobalSkill candidates inside `InsightStore.get_relevant`; add test coverage for cross-repo promotion." |
| §4.4 fleet alerts | W13 | none | "Add `caretaker/fleet/alerts.py` emitting `:FleetAlert` nodes; render in the admin Fleet tab." |
| §4.5 shadow mode | W4 | none | "Add `:ShadowDecision` graph node + a decorator that runs the new LLM path alongside the old heuristic and logs disagreement." |
| §4.6 structured_complete | W3 | none | "Ship `ClaudeClient.structured_complete[T]` with one automatic re-ask on pydantic validation failure; migrate `pr_inline_review` as the canary." |
