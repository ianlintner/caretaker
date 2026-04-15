# Architecture

Caretaker separates **decision-making** from **code authoring**:

- the Python orchestrator reads repository state and decides what should happen
- GitHub Copilot executes code changes through issues, PR comments, and structured instructions

## Main building blocks

### Orchestrator

The orchestrator is the central coordinator in [`src/caretaker/orchestrator.py`](https://github.com/ianlintner/caretaker/blob/main/src/caretaker/orchestrator.py).

It:

- loads config
- routes events to agents
- runs scheduled maintenance passes
- records run summaries

### GitHub client

[`src/caretaker/github_client/api.py`](https://github.com/ianlintner/caretaker/blob/main/src/caretaker/github_client/api.py) wraps the GitHub REST API and returns typed models for issues, PRs, comments, reviews, checks, and repository metadata.

### State tracker

[`src/caretaker/state/tracker.py`](https://github.com/ianlintner/caretaker/blob/main/src/caretaker/state/tracker.py) persists tracking state inside GitHub itself so runs can resume with context.

### Agent modules

Each concern lives in its own package under `src/caretaker/`, which keeps the policy boundaries crisp and the testing surface manageable.

### Goal engine

[`src/caretaker/goals/`](https://github.com/ianlintner/caretaker/tree/main/src/caretaker/goals) adds a quantitative goal-seeking layer on top of agent execution.

It:

- evaluates repository health across measurable goals (for example CI health, PR lifecycle, security posture, and self-health)
- assigns each goal a score from `0.0` to `1.0`
- detects unhealthy trends (critical, diverging, stale)
- produces a goal-driven dispatch plan that prioritizes agents with the highest expected impact
- records per-goal history for trend analysis and escalation

The orchestrator still runs the mode-eligible agents, but the goal engine can reorder execution so urgent work is handled first.

## Workflow model

1. GitHub emits an event or a schedule fires.
2. The workflow runs the `caretaker` CLI.
3. The orchestrator evaluates quantitative goals and computes urgency.
4. The orchestrator selects an agent or full maintenance pass (goal-prioritized when enabled).
5. The chosen agent reads repository state through the GitHub client.
6. The agent creates labels, comments, issues, or PR actions as needed.
7. Run state, goal history, and summaries are persisted for the next execution.

## Why this design works

- **stateless execution** at the workflow layer
- **durable progress tracking** in GitHub itself
- **narrow agent responsibilities** for easier reasoning and testing
- **Copilot as executor** instead of embedding code-writing logic in the orchestrator

In short: the orchestrator decides, Copilot does, GitHub remembers.
