# Agent Architecture Refactor Plan

## Why NOT LangGraph

LangChain/LangGraph is designed for LLM-powered tool-calling agents that
route work through graph-structured conversations. Caretaker is a GitHub
automation pipeline where:

- The "agents" are code modules, not LLM-powered agents
- LLM usage is optional/secondary (Claude for CI log analysis)
- Copilot executes work via structured GitHub comments, not tool calls
- Routing is config/event-driven, not LLM-judgment-driven
- The dependency budget is 6 packages; LangGraph would add 50+

**Verdict:** wrong abstraction, massive overhead, zero benefit.

## What to do instead

Standardize the existing agent architecture using clean agentic patterns:

### Phase 1 — Agent Protocol Layer (`src/caretaker/agent_protocol.py`)

1. **`AgentContext`** — Shared context dataclass replacing repetitive
   `github, owner, repo, config, llm_router` constructor params
2. **`AgentResult`** — Standardized result envelope with:
   - `processed: int` — items examined
   - `actions: list[str]` — human-readable actions taken
   - `errors: list[str]` — errors encountered
   - `state_updates: dict` — optional state mutations
3. **`BaseAgent` ABC** — Common interface with:
   - `name: str` property
   - `enabled(config) -> bool`
   - `async run(context, state) -> AgentResult`

### Phase 2 — Agent Registry (`src/caretaker/registry.py`)

Replace the 11 hardcoded `_run_X_agent()` methods with:

- `AgentRegistry` class with `register()` and `run_agents()` methods
- Each agent self-registers with its name and config key
- Registry handles enabled checks, dry-run, error wrapping, and summary updates

### Phase 3 — Adapt Agents

For each of the 11 agents:

- Add `BaseAgent` subclassing or protocol conformance
- Wrap existing `run()` in the new `AgentResult` return type
- Extract constructor params from `AgentContext`
- Keep the existing internal logic untouched (minimize risk)

### Phase 4 — Refactor Orchestrator

- Replace `_run_pr_agent()`, `_run_issue_agent()`, etc. with registry dispatch
- Collapse the mode-based if/elif chain into registry filtering
- Keep `_handle_event()` and `_reconcile_state()` as-is (agent-specific logic)

### Out of scope

- Changing agent internal logic
- Changing the state persistence model
- Adding new features or LLM capabilities
- Modifying the CLI interface
