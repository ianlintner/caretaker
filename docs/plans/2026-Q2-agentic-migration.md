# Caretaker Master Plan of Record (2026-04-22)

Two parallel audits (fleet-operational + source-engineering) ran this morning. Their raw reports live in `01-fleet-audit.md` and `02-source-audit.md`. This document unifies them into an action plan the next wave of sub-agents can pick up in parallel.

## TL;DR

- **Fleet today.** Five consumer repos; only one (Example-React-AI-Chat-App) is clearly working, two are partially working, two are effectively dark. The three dominant failure modes — spurious non-zero exits triggering self-heal storms, `GITHUB_TOKEN` scope gaps swallowed silently, and a readiness gate that can never clear on solo repos — are all fixable in caretaker itself, not in consumers.
- **Source today.** Architecturally ambitious but drifting. ~40 subsystems added over 3 weeks. The decision layer between GitHub state and caretaker's mechanisms is a stack of if/elif ladders and keyword regexes that should be LLM-synthesised with structured output, exactly as the user flagged. Anthropic prompt caching is not wired up anywhere in the provider layer, costing money on every tool-loop turn.
- **Central thesis.** Stop hand-coding judgement calls. Introduce a `structured_complete[T]` helper + a shadow-mode decorator, move the readiness gate, CI-failure triage, review-comment classification, issue triage, cascade decisions, stuck-PR detection, and executor routing onto the model behind feature flags, and leverage the memory-graph and fleet-graph that are already being written but nothing reads from. Everything else falls out of that.

## Issue inventory (de-duplicated + cross-referenced)

This is the joined list. Fleet-audit findings (F#) are operational; source-audit findings (S#) are code-level.

| ID | Description | Severity | Surface | Cross-ref |
|----|-------------|----------|---------|-----------|
| M1 | Orchestrator exits non-zero on benign tail (missing artifact, stray agent warning) → triggers duplicate self-heal issues every run | blocker | src/caretaker/orchestrator.py + `.github/workflows/maintainer.yml` upload-artifact step | F#1, S#§2.3-adjacent |
| M2 | Caretaker workflow has been red on audio_engineer for a week; pre-orchestrator bootstrap failure leaves no telemetry | blocker | maintainer.yml install step + secret presence checks | F#2, F#8 |
| M3 | Readiness gate requires human approval that never arrives on solo repos; caretaker can't merge its own upgrade PRs | blocker | src/caretaker/pr_agent/states.py:178-259 | F#3, S#§3.1 |
| M4 | `maintainer.yml` template ships without trailing newline → pre-commit `end-of-file-fixer` fails → Copilot fan-out PRs | major | release-sync tool + tests | F#4 |
| M5 | Agents silently swallow 403s on dependabot/code-scanning/secret-scanning/assignees/pulls APIs | major | src/caretaker/security_agent/, docs_agent/, github_client/ | F#5 |
| M6 | Escalation digest is self-referential: lists caretaker-internal self-heal + CI-failure issues caretaker itself created | major | src/caretaker/escalation/ digest builder | F#6 |
| M7 | Self-heal storm: 7+ duplicate issues within 2 minutes on audio_engineer despite a cap claimed in v0.10.0 notes | major | src/caretaker/self_heal_agent/ cap check | F#7 |
| M8 | Orchestrator-state issue balloons to 146 comments; `run-history` + `orchestrator-state` markers append instead of edit-in-place in some paths | minor | src/caretaker/orchestrator.py comment upsert | F#8 |
| M9 | `maintainer:assigned` label applied without actual GitHub assignee (label-assign decoupled from failing POST /assignees) | minor | src/caretaker/pr_agent/agent.py assignee flow | F#9 |
| M10 | Bundled Dependabot PRs (19-package groups) never bisected when one updates breaks CI | major | new dependency_agent capability | F#10 |
| S1 | 10× `datetime.utcnow()` uses produce naive timestamps mixing with tz-aware ones elsewhere | major | src/caretaker/pr_agent/agent.py:178,223,408,584 + peers | S#§2.1 |
| S2 | `pr_number` parameter shadowed by loop variable in `_terminal` block | minor | pr_agent/agent.py:110-150 | S#§2.2 |
| S3 | `MERGE_READY` can be reported for human PRs even when auto-merge disabled | major | pr_agent/states.py:367 | S#§2.3 |
| S4 | `close_duplicate_fix_prs` keeps newest; `close_duplicate_issues` keeps oldest → opposite policies | minor | pr_agent/pr_triage.py:150-167 vs issue_agent/issue_triage.py | S#§2.4 |
| S5 | `tool_loop` fallback builds OpenAI-shape assistant message for Anthropic models → 400s on some provider paths | major | src/caretaker/foundry/tool_loop.py:106-125 | S#§2.5 |
| S6 | Fleet OAuth client cache is a module-level global — cross-tenant leak in multi-config processes | major | src/caretaker/fleet/emitter.py:58-97 (code **I shipped** in v0.12.0 — self-inflicted) | S#§2.6 |
| S7 | `_has_pending_task_comment` assumes comment ordering from the API | minor | pr_agent/agent.py:450-459 | S#§2.7 |
| E1 | No Anthropic prompt caching anywhere — every tool-loop turn resends the full system prompt | major | src/caretaker/llm/provider.py:158-178 | S#§4.1 |
| E2 | `AgentCoreMemory` graph node is written every run but nothing reads it back on the next run | major | src/caretaker/memory/core.py + new retriever.py | S#§4.2 |
| E3 | `:GlobalSkill` fleet promotion is write-only — promoted skills never flow back into prompts | major | src/caretaker/fleet/graph.py + llm prompt builders | S#§4.3 |
| E4 | Fleet dashboard renders heartbeats but does not alert on goal_health regression | minor | new src/caretaker/fleet/alerts.py + frontend Fleet tab | S#§4.4 |
| D1 | No shadow-mode infrastructure — every LLM handover is a leap of faith | blocker for migrations | new :ShadowDecision graph node + decorator | S#§4.5 |
| D2 | `pr_reviewer/inline_reviewer.py` raw json.loads with silent downgrade on parse error | major | need `structured_complete[T]` helper | S#§4.6 |

## Agentic migration targets (from source §3)

Each of these hand-rolled heuristics is an LLM decision with a structured-output schema in waiting. The schemas below are the contract; the sub-agent prompts in §Parallelization map implement them.

| Tag | Current code | Schema |
|-----|--------------|--------|
| A1 | `pr_agent/states.py` readiness rubric | `Readiness(verdict, confidence, blockers[], summary)` |
| A2 | `.github/workflows/maintainer.yml` + `pr_agent/_constants.py` dispatch-guard actors/markers | `DispatchVerdict(is_self_echo, is_human_intent, suggested_agent?)` |
| A3 | `pr_agent/ci_triage.py` keyword ladder | `FailureTriage(category, confidence, is_transient, root_cause_hypothesis, minimal_reproduction, suggested_fix, files_to_touch[])` |
| A4 | `pr_agent/review.py` `classify_review_basic` | `ReviewClassification(kind, severity, summary_one_line, requires_code_change, suggested_prompt_to_copilot?)` |
| A5 | `issue_agent/classifier.py` + `issue_triage.py` keyword+hash | `IssueTriage(kind, labels[], duplicate_of?, staleness, confidence)` |
| A6 | `pr_agent/cascade.py:132-184` cascade heuristic | `CascadeAction(action: redirect\|close\|keep_open, justification)` |
| A7 | Five places string-matching bot logins | `BotIdentity(is_automated, family?)` memoized |
| A8 | `pr_agent/agent.py:196-216` stuck-PR age threshold | `StuckVerdict(is_stuck, stuck_reason, recommended_action, explanation)` |
| A9 | `pr_reviewer/routing.py` + `foundry/size_classifier.py` point systems | `ExecutorRoute(path, reason, risk_tags[])` |
| A10 | `evolution/crystallizer.py` category regex | reuse A3 |

## Places where LLM is doing deterministic work (from fleet §Agentic-vs-bizlogic)

These go the other direction: pull them **out** of the LLM path and back into deterministic Python.

- Trailing-newline fix on `maintainer.yml` (→ one-line Python, no Copilot round-trip). Ties to M4.
- `.github/maintainer/.version` bump to apply an upgrade (→ `sed -i`, no Copilot issue). Ties to upgrade_agent.
- Readiness-comment templating (→ pure string rendering, delete the LLM fallback path for this).
- Comment marker insertion (→ already deterministic, keep it).

## Phased rollout

### Phase 1 — Foundations + fleet triage (weeks 1–2)

Goal: unblock everything downstream with the migration scaffolding, fix the bleeding on live consumer repos, land all correctness bugs.

Parallel workstreams:

- **W1 — Correctness bugs.** Ship S1–S7 and M8 as small independent PRs. Author: one sub-agent, serialized commits, parallel PR review. Exit: all seven merged and released as `0.12.1`.
- **W2 — Prompt caching + cache-hit metric (E1).** Add `cache_control: ephemeral` to Anthropic and LiteLLM provider call paths, emit `caretaker_llm_cache_hit_ratio` Prometheus metric, back it with `usage.cache_read_input_tokens`. Exit: ≥50% cache-hit ratio on a synthetic 20-iteration FoundryExecutor loop.
- **W3 — `structured_complete[T: BaseModel]` helper (D2).** One automatic pydantic-validation re-ask; canary migrate `pr_reviewer/inline_reviewer.py`. Exit: inline reviewer ships, raw json.loads gone.
- **W4 — Shadow-mode infra (D1).** `ShadowDecision` Neo4j node + `@shadow_decision(name)` decorator that runs old + new side-by-side, logs disagreement. Admin UI tab. Exit: one feature (inline reviewer) has shadow data flowing.
- **W5 — Fleet bleeding fixes (M1, M2, M5, M7).** Make `orchestrator` return exit 0 when all agent errors are in a known-transient bucket; make artifact upload `if-no-files-found: ignore`; add an explicit `caretaker doctor` pre-run check that fails loudly on missing secrets + missing scopes; plug the self-heal storm cap by keying on `(repo, error_signature)` instead of just `error_signature`. Exit: audio_engineer green on schedule; no more "Unknown caretaker failure" self-heal storms fleet-wide.

Phase 1 is the only phase where bleeding-edge migrations (Phase 2) are blocked on predecessor work (W3+W4). Everything else is independent — five sub-agents in parallel.

### Phase 2 — Decision migrations (weeks 3–5)

Goal: move A1–A10 onto the model, each gated `agentic.<domain> = off | shadow | enforce`, with ≥1 week of shadow data before flipping to enforce.

Parallel workstreams:

- **W6 — Readiness (A1, M3).** `Readiness` model + flag; solves "solo repo can't merge" by design because the model sees the repo shape.
- **W7 — Failure triage (A3, A10).** One shared classifier for CI failures and crystallizer categories.
- **W8 — Review-comment classification (A4).**
- **W9 — Issue triage + dup detection (A5).** Keep CVE regex as deterministic pre-filter.
- **W10 — Cascade + stuck-PR (A6, A8).** Share context with readiness (W6) for solo-repo awareness.
- **W11 — Bot-identity consolidation (A7).** Prerequisite for W14.
- **W12 — Escalation-digest rewrite (M6).** Model-authored weekly digest that filters out caretaker's own exhaust.

Phase 2 can be parallelized at the workstream level. W10 depends on W6 for the solo-repo context model.

### Phase 3 — Read-side leverage + dispatch migration (week 6)

Goal: unlock the data caretaker is already writing.

- **W13 — Cross-run memory retrieval (E2).** `memory/retriever.py`; injected into W6's readiness call under a 500-token budget. Exit: one merged PR attributable to a retrieved past-skill hint.
- **W14 — Skill promotion round-trip (E3).** Union `:Skill` ∪ `:GlobalSkill` in `InsightStore.get_relevant`. Exit: a skill promoted on one repo appears in another repo's next prompt.
- **W15 — Fleet alerts (E4).** `:FleetAlert` node + admin UI. Exit: goal_health regression triggers an alert on a staged repo.
- **W16 — Dispatch-guard LLM migration (A2).** Depends on W11.
- **W17 — Dependabot bisector (M10).** Split-fix for broken grouped Dependabot PRs; can be its own agent.

### Research spikes (timeboxed, can run anytime in Phase 1–2)

- R1 — GitHub App as primary runtime vs scheduled workflow (2 days). Remove races that motivate half the current safeguards.
- R2 — Prompt-cache TTL vs tool-loop length (1 day). Measure; may motivate loop-chunking.
- R3 — Vector store for skill promotion (2 days). pgvector vs Neo4j vector vs Qdrant; recall@5 on 500 rows.
- R4 — Is PRTrackingState enum still load-bearing once A1+A8 ship? (1 day).
- R5 — OTel span provenance effect on reflection prompt quality (1 day).

## Parallelization map (for sub-agent fan-out)

Every row is one sub-agent's job. Columns: id, workstream, preconditions (workstream IDs), seed prompt fragment. Sub-agents are expected to produce a PR + tests + CHANGELOG entry + passing CI.

| ID | WS | Preconditions | Seed |
|----|----|---------------|------|
| T-S1 | W1 | — | Replace every `datetime.utcnow()` in `src/caretaker/` with `datetime.now(UTC)`; remove now-redundant `_as_utc` wraps; preserve serialized tz-naive compat where pydantic state models round-trip from YAML. |
| T-S2 | W1 | — | Rename the shadowed loop variable in `pr_agent/agent.py`'s `_terminal` block so the outer `pr_number` parameter stays bound. |
| T-S3 | W1 | T-S1 | Split PR states so `MERGE_READY` only fires when `auto_merge.<profile>` permits; status comment must never claim "ready for merge" under a disabled profile. |
| T-S4 | W1 | — | Align `pr_triage.close_duplicate_fix_prs` survivor selection with `issue_triage.close_duplicate_issues` (oldest wins); update tests + docstring cross-reference. |
| T-S5 | W1 | — | Remove the OpenAI-shaped fallback in `foundry/tool_loop.py`; require providers to always populate `raw_message`; add a regression test. |
| T-S6 | W1 | — | Replace the module-global OAuth client cache in `fleet/emitter.py` with an instance on `Orchestrator`; thread through `emit_heartbeat`; add multi-tenant regression test. |
| T-S7 | W1 | — | Sort comments by `(created_at, id)` inside `_has_pending_task_comment`; regression test with reversed-order input. |
| T-M1 | W5 | — | Make orchestrator exit 0 when all agent errors are in a known-transient bucket (403s, empty-artifact, upstream 5xx); emit `orchestrator_soft_fail_total` counter so the signal stays visible. |
| T-M2 | W5 | T-M1 | Add a `caretaker doctor` preflight run inside maintainer.yml that fails loudly on missing secrets + missing scopes before any agent boots; produce a structured "required scopes" report. |
| T-M4 | W5 | — | Ensure every file written by the release-sync tool ends with `\n`; add a golden-file test on `maintainer.yml`; close the Copilot-EOF-newline fan-out root cause. |
| T-M5 | W5 | — | Surface 403s from `dependabot/alerts`, `code-scanning/alerts`, `secret-scanning/alerts`, `POST /pulls`, `POST /assignees` as a single "scope gap" issue per run instead of five silent warnings. |
| T-M7 | W5 | — | Change self-heal cap key from `error_signature` to `(repo, error_signature, hour_window)`; add a storm-replay test. |
| T-M8 | W1 | — | Fix orchestrator-state issue comment upsert so `run-history` + `orchestrator-state` markers always edit-in-place; test by driving 20 runs and asserting comment count ≤ 2. |
| T-E1 | W2 | — | Add `cache_control: {type: "ephemeral"}` to the system block in both `AnthropicProvider.complete` and `LiteLLMProvider.complete_with_tools`; emit `caretaker_llm_cache_hit_ratio`. |
| T-D2 | W3 | T-E1 | Ship `ClaudeClient.structured_complete[T: BaseModel]` with one auto-re-ask on pydantic validation failure; migrate `pr_reviewer/inline_reviewer.py` as canary. |
| T-D1 | W4 | — | Add `:ShadowDecision` graph node + `@shadow_decision(name)` decorator; render diff report in admin UI Fleet/Shadow tab. |
| T-A1 | W6 | T-D2, T-D1 | Readiness migration: pydantic model + `agentic.readiness` flag + shadow; ship behind `off` default. |
| T-A3 | W7 | T-D2 | CI failure triage migration with `FailureTriage`; delete keyword-ladder when shadow disagreement <5%. |
| T-A4 | W8 | T-D2 | Review-comment classification with `ReviewClassification`; propagate severity downstream. |
| T-A5 | W9 | T-D2 | Issue triage + dup with `IssueTriage`; keep CVE regex as deterministic pre-filter; wire embedding-similarity for dup candidates. |
| T-A6 | W10 | T-D2 | Cascade migration (`close_linked_issues`/`redirect`) with `CascadeAction`; preserve deterministic linked-issue parser. |
| T-A8 | W10 | T-D2, T-A1 | Stuck-PR judgement with `StuckVerdict`; `stuck_age_hours` stays as min-age pre-filter. |
| T-A7 | W11 | — | Consolidate all `[bot]`/`copilot` login checks behind `caretaker.identity.is_automated(login)`; memoize per-login; used by W14. |
| T-M6 | W12 | T-A5 | Rewrite weekly escalation digest as model-authored summary that explicitly excludes caretaker-authored self-heal + CI-failure issues unless they contain novel signal. |
| T-E2 | W13 | T-A1 | `memory/retriever.py` cosine-over-embedded-summary; inject top-3 past snapshots into readiness prompt ≤500 tokens. |
| T-E3 | W14 | — | Union `:Skill` ∪ `:GlobalSkill` in `InsightStore.get_relevant`; cross-repo test. |
| T-E4 | W15 | — | `caretaker/fleet/alerts.py` + admin Fleet tab; goal_health regression threshold. |
| T-A2 | W16 | T-A7 | Move dispatch-guard from workflow YAML into caretaker; regex stays as cheap prefilter, LLM for ambiguous cases; timeout fallback to old behaviour. |
| T-M10 | W17 | — | Dependabot bisector: on a `MERGEABLE UNSTABLE` grouped PR, split by sub-package, retry CI per subset, propose a surgical merge plan. |
| T-R1..T-R5 | spikes | — | One-pager design docs as described in §Research spikes. |

## Supervisor loop

A thin supervisor runs the fan-out:

1. Claim the next unblocked task (lowest ID) from the parallelization map.
2. Dispatch a sub-agent with the seed prompt + a reference to `00-master-plan.md`, `01-fleet-audit.md`, `02-source-audit.md`, and the relevant file paths.
3. The sub-agent returns a PR URL + a short note on whether it hit a precondition the map didn't capture.
4. CI green → merge; CI red → re-dispatch with the failure context.
5. Each merged task triggers a quick release (`0.12.x` during Phase 1, `0.13.x` in Phase 2, `0.14.0` at Phase 3 exit).

The supervisor itself is a reasonable candidate for its own sub-agent in a follow-on iteration.

## Open risks

- **Readiness-gate migration (T-A1) is the hottest path.** If the model under-blocks, caretaker starts merging work that shouldn't merge. Shadow data gating is mandatory; enforce-mode flip gated on human review.
- **Prompt-cache TTL (R2).** Tool loops regularly exceed 5 min; cache wins may be smaller than expected on long-running Foundry runs. Measure before claiming savings.
- **M4 + M5 are simple code fixes but each unblocks large fleet-wide silences.** Do not save them for Phase 2.
- **S6 is self-inflicted** — shipped in v0.12.0 this morning. Patch in Phase 1 W1 or mark the v0.12.0 docs.

### Per-site model overrides (2026-04-22)

`agentic.<site>.model_override` (and its sibling `max_tokens_override`)
lets the nightly-eval harness A/B different LLM models against the same
legacy heuristic on a single decision site. When the override is set,
the candidate leg of `@shadow_decision` receives a `model=` kwarg that
propagates into `ClaudeClient.structured_complete` /
`ClaudeClient.complete`; the legacy leg, and every LLM call outside
shadow decisions, continues to use `llm.default_model`. Leave the
override at `None` to inherit the router default.

**Two-model A/B pattern (recommended rollout):**

1. Site already running in `mode: shadow` on `llm.default_model` (call
   it *model A*) for at least 7 days; baseline agreement-rate
   established in the Braintrust dashboard.
2. Set `agentic.<site>.model_override: "<model B>"`. Leave the site in
   `mode: shadow` for another 7 days. The candidate leg now runs on
   model B; the legacy leg is unchanged; every `:ShadowDecision` row
   stamps `candidate_model = "<model B>"`.
3. Compare the two windows in Braintrust. The harness surfaces
   `per_model_reports` in the nightly JSON report and logs an
   `eval site=... candidate_model=... agreement_rate=...` line per
   model when the window is mixed, so the split shows up both in the
   CI comment and in the admin dashboard.
4. If model B's agreement rate ≥ model A's (and the disagreement
   samples look qualitatively better), clear `model_override` — model A
   is now the router default so the enforce-mode flip will use it —
   OR promote model B to `llm.default_model` and clear the override.
   Either way, the override is removed once the A/B concludes so the
   site returns to a single-model shadow stream.

**Known follow-up:** the enforce-gate's `min_agreement_rate` check
(see `AgenticEnforceGateConfig.min_agreement_rate`) applies per-site
and does *not* distinguish candidate models. If model B is winning in
the A/B but the site-wide rolling mean is dragged down by model A's
earlier samples, the gate may refuse to unlock until the window rolls
far enough forward. A future refinement is a per-model
`min_agreement_rate` map (e.g. `{"azure_ai/gpt-5": 0.93}`) so the
enforce flip can be gated specifically on the model about to become
authoritative. Not in scope for this change — tracked as a separate
follow-up on the 2026-Q2 migration plan.
