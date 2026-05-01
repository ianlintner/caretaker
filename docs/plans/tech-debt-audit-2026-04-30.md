# Caretaker Tech Debt Audit — 2026-04-30

Comprehensive audit covering code, architecture, tests, dependencies, infra, docs, and agentic patterns. Findings verified against source (e.g., `pr_agent/agent.py` = 2091 LOC; `principal_agent/agent.py:3` literally says "PLACEHOLDER: not yet implemented"; `anthropic>=0.40,<1` is ~50 minor versions stale).

**Key insight:** The codebase is unusually disciplined for its scale (only ~5 real TODO markers across 275 files, 22 type-ignores, ruff+mypy strict in pre-commit) — the debt is **structural** (god-modules, dual agent protocols, missing cost/safety primitives), not **rotted** (stale code, dead tests).

---

## 1. Findings by Category

### Code Debt
| ID | Item | File:Evidence |
|----|------|---------------|
| C1 | God-module: PR agent | `pr_agent/agent.py` = **2091 LOC**, 19 internal imports, hub for triage + readiness + merge + cascade + CI |
| C2 | God-module: config | `config.py` = **1647 LOC**, 71 defs, all 18 agent configs + LLM + guardrails in one file |
| C3 | God-module: GitHub client | `github_client/api.py` = **1697 LOC**, no domain split |
| C4 | God-module: doctor | `doctor.py` = **2075 LOC** diagnostic surface |
| C5 | God-module: CLI | `cli.py` = **1463 LOC**, tightly coupled to orchestrator |
| C6 | Duplicated GitHub-write helpers | `github_client/api.py` AND `tools/github.py` both implement `post_comment`/`add_label`; agents call both inconsistently |
| C7 | Per-agent reinvention | Comment dedup, ownership checks, report shapes hand-rolled in each `*_agent/agent.py` |

### Architecture Debt
| ID | Item | Evidence |
|----|------|----------|
| A1 | **Dual agent protocols** | `agent_protocol.py` `BaseAgent(ABC)` adopted by only **6/18 agents**; 12 still duck-typed with divergent `run()` signatures |
| A2 | Cross-agent imports | `pr_agent/triage_adapter.py` imports `caretaker.issue_agent.issue_triage` directly — agents calling agents |
| A3 | No infra/agent boundary | Agents pull directly from `state`, `memory`, `graph`, `runs`, `scheduler`; no facade |
| A4 | Half-finished refactor | `migration_agent`, `perf_agent`, `principal_agent`, `refactor_agent` inherit `BaseAgent` but have **no adapters** |
| A5 | `principal_agent` is a stub | `principal_agent/agent.py:3` literally `"PLACEHOLDER: not yet implemented"` |

### Test Debt
| ID | Item | Evidence |
|----|------|----------|
| T1 | Inconsistent test layout | Some agents have `tests/test_<agent>/`, most are flat `tests/test_<agent>.py` |
| T2 | Single mega-file | `tests/test_pr_agent/test_agent.py` = **2950 LOC**, 32 mocks |
| T3 | Mock-heavy | **1472 patch/MagicMock** uses across `tests/`; 77 in `test_github_client_api.py` alone |
| T4 | Almost no E2E | Only 2 modest end-to-end files (`test_consensus_e2e.py`, `test_eventbus_webhook_integration.py`) |
| T5 | Smoke set is hardcoded | `.pre-commit-config.yaml` hardcodes 4 smoke tests; doesn't auto-track |

### Dependency Debt
| ID | Item | Evidence |
|----|------|----------|
| D1 | **Anthropic SDK pin is ~50 minor versions stale** | `pyproject.toml`: `anthropic>=0.40,<1` (released ~Q4 2024); blocks adoption of extended thinking, batch API, native structured output, memory tool |
| D2 | CVE override workarounds in pyproject | `python-dotenv>=1.2.2` (CVE-2025-14974), `aiohttp>=3.13.5` (CVE-2026-34516, CVE-2024-52304) — pre-emptive pins suggest ongoing pressure |
| D3 | **No `dependabot.yml`** | Manual dep review — ironic for a tool whose flagship is the Dependency Agent |

### Infra / Ops Debt
| ID | Item | Evidence |
|----|------|----------|
| I1 | No CI parallelism | `ci.yml` runs lint+test serially; no matrix; 11 separate workflows with no coordination |
| I2 | No resource limits in k8s manifests visible | Probes present, requests/limits not |
| I3 | Pre-commit missing scanners | No detect-secrets, no semgrep, no license check |
| I4 | Hand-curated CHANGELOG | 1067 LOC, no generator — high manual effort |

### Agentic-System Gaps (2026 best-practice deltas)
| ID | Gap | Impact |
|----|-----|--------|
| G1 | **No tiered model routing** | All work goes to Sonnet/Opus regardless of difficulty; no Haiku tier for cheap classification — significant cost overspend |
| G2 | **No cost dashboard** | Cost tracked per call but not aggregated per-repo / per-agent / per-model — operators are blind to drivers |
| G3 | **No human-in-the-loop queue** | All decisions autonomous; no approval gate for risky actions (force merges, destructive cleanups) |
| G4 | **No dry-run / replay mode** | Cannot safely test new agents/prompts on real events |
| G5 | **No prompt versioning** | Prompts inline in `.py`; no `.md` library, no A/B harness; consensus engine could exploit this |
| G6 | **No drift detection** | Agent decisions vs human merges not compared post-hoc |
| G7 | **No reasoning audit trail** | Operators get metrics but not "why did agent X choose Y?" — Braintrust shadow-decisions exist but no UI |
| G8 | **No parallel tool execution / streaming** | `foundry/tool_loop.py` is sequential & blocking |
| G9 | **No extended thinking** | Anthropic SDK too old; refactor/architecture decisions would benefit |
| G10 | **MCP is server-only** | Caretaker exposes data via MCP but doesn't consume external MCPs (e.g., Linear, Slack, Sentry) |
| G11 | **No codebase-as-context indexing** | Fresh fetch per dispatch; no embedded vector index of consumer repos |
| G12 | **No per-agent budget caps** | Token limits per loop, but no $ cap per agent per repo per day |

### Documentation Debt
- 27 docs + 17 plans + 1067-line handwritten CHANGELOG = **maintenance burden** (D4)
- Some plan docs (e.g., orchestrator self-chaining) describe features that **already shipped** in commits 5ad53ff/521e178 — drift between plan and code

---

## 2. Prioritization Matrix

Score = (Impact + Risk) × (6 − Effort). Range: 2–50.

| ID | Item | Impact | Risk | Effort | **Score** |
|----|------|:-:|:-:|:-:|:-:|
| D1 | Bump Anthropic SDK pin (unblocks G8/G9/several others) | 5 | 4 | 2 | **36** |
| D3 | Add `dependabot.yml` | 3 | 4 | 1 | **35** |
| I3 | Add detect-secrets + semgrep to pre-commit | 2 | 4 | 1 | **30** |
| A1 | Unify agent protocol (all 18 inherit BaseAgent, consistent signature) | 5 | 4 | 3 | **27** |
| G2 | Cost dashboard (per repo/agent/model) | 5 | 3 | 3 | **24** |
| G1 | Tiered model routing (Haiku/Sonnet/Opus by complexity) | 5 | 3 | 3 | **24** |
| C2 | Split `config.py` per-agent module + factory | 4 | 3 | 3 | **21** |
| I1 | CI matrix + parallel jobs | 3 | 2 | 2 | **20** |
| G3 | Human-in-the-loop approval queue for destructive actions | 4 | 5 | 4 | **18** |
| C6/C7 | Lift comment/label/dedup helpers into shared GitHub service | 3 | 3 | 3 | **18** |
| A4 | Finish or remove half-refactored agents (perf/migration/refactor) | 3 | 3 | 3 | **18** |
| G6 | Drift detection (agent vs human decision comparator) | 3 | 3 | 3 | **18** |
| G5 | Prompt template library + versioning | 3 | 2 | 3 | **15** |
| C1 | Decompose `pr_agent/agent.py` (extract ci_triage, shepherd, ownership as composables) | 4 | 3 | 4 | **14** |
| G4 | Dry-run / replay mode | 4 | 3 | 4 | **14** |
| T2 | Split 2950-LOC PR agent test into modules | 2 | 2 | 2 | **16** |
| I4 | Auto-generate CHANGELOG from commits | 2 | 1 | 2 | **12** |
| G7 | Reasoning audit trail / "why did agent X" UI | 3 | 3 | 4 | **12** |
| T3 | Reduce mock-heavy tests; add contract tests vs `responses`/`respx` | 3 | 3 | 4 | **12** |
| G10 | Add MCP client capability (consume external MCPs) | 3 | 1 | 3 | **12** |
| A5 | Implement or delete `principal_agent` | 3 | 2 | 4 | **10** |
| C3/C4/C5 | Decompose `github_client/api.py`, `doctor.py`, `cli.py` | 3 | 2 | 4 | **10** |
| G11 | Codebase-as-context indexing | 3 | 2 | 5 | **5** |

---

## 3. Phased Remediation Plan

### Phase 0 — Quick Wins (1 sprint, alongside features)
- **D1** Bump `anthropic` to current version; remove `<1` cap; pin floor at a known-good version. Run full test suite. Unblocks Phase 2/3.
- **D3** Add `.github/dependabot.yml` for `pip`, `github-actions`, `docker` ecosystems.
- **I3** Add `detect-secrets` and `semgrep` to `.pre-commit-config.yaml`.
- **I1** Split `ci.yml` into parallel `lint` / `typecheck` / `unit` / `integration` jobs.
- **A5** Decision: delete `principal_agent/` package or open a tracking issue scoped to v0.x.
- **D4 (docs)** Sweep `docs/plans/` and mark shipped plans as **DONE** with commit links; move stale ones to `docs/plans/archive/`.

### Phase 1 — Architectural Foundations (3–4 sprints)
- **A1** Migrate the 12 duck-typed agents to `BaseAgent` with a unified `async execute(state, payload) -> AgentResult` signature. Land one agent per PR for low review risk.
- **C6/C7** Introduce `caretaker.github_service` facade; consolidate `post_comment`, `add_label`, dedup, and ownership. Migrate agents incrementally.
- **C2** Split `config.py` into `caretaker.config.{agents,llm,guardrails,infra}` submodules; keep public `Config` import path stable.
- **A4** Finish or remove `migration_agent`/`perf_agent`/`refactor_agent` — pick one path per agent; document in `CHANGELOG.md`.

### Phase 2 — Cost & Safety (2–3 sprints; depends on D1)
- **G1** Implement `LLMRouter.route(complexity)` with explicit Haiku/Sonnet/Opus tiers; consensus engine is the natural integration point.
- **G2** Cost dashboard: aggregate `LLMResponse.cost_usd` by `(repo, agent, model, day)`; expose `/api/admin/costs` and a dashboard tile.
- **G12** Per-agent daily $ caps in `guardrails/`; trip → escalation issue.
- **G3** Human-in-the-loop queue: introduce `requires_approval` predicate + `/api/admin/approvals` queue for destructive actions (force-merge, mass close).

### Phase 3 — Modern Anthropic & Agentic Features (4–6 sprints)
- **G8** Streaming + parallel tool execution in `foundry/tool_loop.py`.
- **G9** Extended thinking for `refactor_agent`, `principal_agent` (or its replacement), and architecture review.
- **G5** `/prompts/*.md` template library with frontmatter (model, version, owner) + Braintrust A/B harness.
- **G6** Drift detector: `evolution/` already collects shadow decisions — extend to compare agent vs human merge outcome; emit weekly digest.
- **G7** Operator UI tab in admin dashboard: agent decision trace (consensus votes, retrieved memories, tool calls).
- **G10** Add MCP **client** capability to consume Linear/Slack/Sentry MCPs.

### Phase 4 — Scale-out (later, 3–4 sprints)
- **G11** Codebase-as-context: index consumer-repo top-N files in vector DB on each `caretaker run`; gate on cost/value.
- **C1/C3/C4/C5** God-module decomposition (PR agent, GitHub client, doctor, CLI). Highest absolute LOC, lowest urgency once protocol/facade work above lands — a unified protocol makes these splits safer.

### Continuous (every sprint)
- **T2/T3** When touching an agent for any reason, split tests >800 LOC and replace deep-mocks with `respx`/`responses` contract stubs.
- **I4** Add `git-cliff` or similar; convert CHANGELOG to generated.

---

## 4. Business Justification (highlights)

- **D1 + G1 (cost tier)** — likely 30–60% LLM cost reduction on classification-heavy agents (issue triage, dependency PR review). Pays for itself fast as fleet adoption grows.
- **G3 (HITL)** — single bad autonomous action against a real user repo is a brand event; cheap insurance.
- **A1 (unified protocol)** — removes the largest barrier to onboarding new agents (currently authors must reverse-engineer 18 different shapes); accelerates Phase 2/3.
- **G2 + G6 (cost dash + drift)** — required for any operator who wants to *trust* the system enough to widen autonomy.
- **D3 (dependabot)** — credibility: shipping a dep-review agent without dep automation on your own repo is awkward.

---

## 5. Next Steps

- **Phase 0** is a natural entry point (≈1 sprint, low risk).
- **Key architectural decision:** Phase 1 A1 (agent protocol unification) can follow three paths:
  - **Option A: Hard cutover** — define `AgentResult`, migrate all 18 in one PR train, delete duck-typed paths. Faster, single review surface, scary.
  - **Option B: Adapter layer** — keep both protocols, wrap legacy agents in an adapter that conforms to `BaseAgent`, migrate over months. Safer, but dual-protocol pain persists.
  - **Option C: Agent-by-agent, one per PR** — pragmatic middle path; migrate 1 per PR with shared review pattern. Each PR small, but coordination cost is real.
- Choose based on team risk budget and available effort.
