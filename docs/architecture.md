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

## Workflow model

1. GitHub emits an event or a schedule fires.
2. The workflow runs the `caretaker` CLI.
3. The orchestrator selects an agent or full maintenance pass.
4. The chosen agent reads repository state through the GitHub client.
5. The agent creates labels, comments, issues, or PR actions as needed.
6. Run state and summaries are persisted for the next execution.

## Why this design works

- **stateless execution** at the workflow layer
- **durable progress tracking** in GitHub itself
- **narrow agent responsibilities** for easier reasoning and testing
- **Copilot as executor** instead of embedding code-writing logic in the orchestrator

In short: the orchestrator decides, Copilot does, GitHub remembers.
