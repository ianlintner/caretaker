# Configuration

Caretaker configuration is defined by the `MaintainerConfig` model in [`src/caretaker/config.py`](https://github.com/ianlintner/caretaker/blob/main/src/caretaker/config.py).

## Main files

- config file: `.github/maintainer/config.yml`
- JSON schema: [`schema/config.v1.schema.json`](https://github.com/ianlintner/caretaker/blob/main/schema/config.v1.schema.json)
- default template: [`setup-templates/templates/config-default.yml`](https://github.com/ianlintner/caretaker/blob/main/setup-templates/templates/config-default.yml)

## Validation

Caretaker uses strict validation (`extra = forbid`), so unknown keys fail fast instead of silently doing something regrettable.

Validate before committing:

```bash
caretaker validate-config --config .github/maintainer/config.yml
```

## Top-level sections

| Section            | Purpose                                               |
| ------------------ | ----------------------------------------------------- |
| `version`          | schema version, currently `v1`                        |
| `orchestrator`     | schedule and summary behavior                         |
| `pr_agent`         | PR monitoring, merge policy, CI retry behavior        |
| `issue_agent`      | issue triage and auto-assignment                      |
| `devops_agent`     | CI failure detection on the default branch            |
| `self_heal_agent`  | caretaker-on-caretaker failure diagnosis              |
| `security_agent`   | Dependabot, code scanning, and secret scanning triage |
| `dependency_agent` | dependency PR merging and digest generation           |
| `docs_agent`       | changelog/docs reconciliation                         |
| `charlie_agent`    | janitorial cleanup for caretaker-managed work         |
| `stale_agent`      | stale issues, PRs, and merged branch cleanup          |
| `upgrade_agent`    | caretaker release upgrade strategy                    |
| `human_escalation` | digest issue for work needing maintainer action       |
| `escalation`       | escalation targets and stale-age policy               |
| `llm`              | optional Claude feature toggles                       |
| `goal_engine`      | experimental goal-driven agent dispatch               |
| `memory_store`     | persistent agent memory configuration                 |
| `azure`            | azure environment settings (e.g. managed identity)    |
| `mcp`              | optional remote mcp server connections                |
| `telemetry`        | optional app insights instrumentation                 |

## Example

```yaml
version: v1

orchestrator:
  schedule: daily
  summary_issue: true

pr_agent:
  auto_merge:
    copilot_prs: true
    dependabot_prs: true
    merge_method: squash
  copilot:
    max_retries: 2
  ci:
    flaky_retries: 1

issue_agent:
  auto_assign_bugs: true
  auto_assign_features: false

security_agent:
  min_severity: medium

charlie_agent:
  stale_days: 14
  close_duplicate_issues: true
  close_duplicate_prs: true

stale_agent:
  stale_days: 60
  close_after: 14

goal_engine:
  enabled: false # Experimental feature
  goal_driven_dispatch: false
  divergence_threshold: 3
  stale_threshold: 5
  max_history: 20

memory_store:
  enabled: true
  db_path: .caretaker-memory.db
  snapshot_path: .caretaker-memory-snapshot.json
  max_entries_per_namespace: 1000

azure:
  use_managed_identity: false

mcp:
  enabled: false
  endpoint: "http://caretaker-mcp.caretaker.svc.cluster.local:80"
  auth_mode: "none"
  timeout_seconds: 30
  allowed_tools: ["example_tool"]

telemetry:
  enabled: false
  application_insights_connection_string_env: "APPLICATIONINSIGHTS_CONNECTION_STRING"
```

## Notes on behavior

### PR agent

The PR agent combines CI status, review state, and configured retry policy to decide whether to wait, request fixes, escalate, or merge.

### Security and dependency agents

These agents are intentionally conservative:

- dependency auto-merge is limited to patch/minor policies
- security triage respects a minimum severity threshold
- false positive rules can suppress noisy alerts

### Docs and stale agents

The docs agent updates changelog-style documentation from recently merged PRs, while the stale agent warns and closes aged work based on repo policy.

### Charlie agent

The Charlie agent is a narrower janitor for caretaker-managed operational work. It closes duplicate assignment issues/PRs and short-lived abandoned automation after a smaller default window than the generic stale agent.

### Goal engine

The goal engine is an **experimental** feature that evaluates quantitative repository health goals and can reorder agent dispatch based on which goals need the most attention.

When `enabled: true` but `goal_driven_dispatch: false`, it only evaluates and tracks goals without changing agent order.

When both are `true`, agents are reordered to prioritize work that improves the worst-scoring goals.

See the [goals documentation](goals.md) for details.

### Memory store

The memory store provides persistent, disk-backed storage for agent state that needs to survive across orchestrator runs.

It's primarily used for:

- Deduplication signatures that prevent creating duplicate issues
- Cooldown timers for rate-limited actions
- Agent-specific state that doesn't fit in the GitHub-backed state tracker

The SQLite database file and JSON snapshot are typically excluded from git via `.gitignore`.

### Remote MCP and Azure Integrations

Caretaker supports optional remote capability expansion via Model Context Protocol (MCP) and Azure. This allows caretaker to execute heavy, shared, or private-network capabilities remotely on an AKS cluster or Azure Container App while keeping the core orchestrator local or in GitHub Actions.

- `mcp`: Configures connection to a remote MCP backend service. Must be enabled to route capabilities.
- `azure`: Allows using Azure Managed Identity for secure access to remote backends and resources.
- `telemetry`: Enables telemetry scaffolding for remote tool calls and latency. At present this is stub/debug instrumentation and does not export data to Azure Application Insights.

## LLM provider selection

The `llm` block selects which provider executes Caretaker's analysis
features (CI log triage, architectural review, upgrade impact, etc.).

### Supported providers

| `provider` | Description |
| --- | --- |
| `anthropic` | Direct Anthropic SDK with `ANTHROPIC_API_KEY`. Default. |
| `litellm` | Multi-provider routing (Anthropic, OpenAI, Vertex, Azure OpenAI, Azure AI Foundry, Bedrock, Ollama, Mistral, Cohere, Groq). Each provider's env vars apply. |
| `openrouter` | LiteLLM under the hood, but pinned to OpenRouter via `OPENROUTER_API_KEY`. Strict `openrouter/`-prefix model strings. |

### Per-feature model routing

`feature_models` overrides the model used for a specific analysis
feature. Caretaker ships a per-provider default map; operator entries
win over both.

```yaml
llm:
  provider: openrouter
  default_model: openrouter/anthropic/claude-sonnet-4.6
  feature_models:
    ci_log_analysis:
      model: openrouter/deepseek/deepseek-r1
      max_tokens: 2500
    architectural_review:
      model: openrouter/anthropic/claude-opus-4.6
```

### Strict prefix rule under `provider: openrouter`

When `provider` is `openrouter`, every model string in `default_model`,
every `feature_models[*].model`, and every `fallback_models` entry must
begin with `openrouter/`. Caretaker raises a `ValidationError` at
config-load otherwise. The error names every offending field with a
fix suggestion.

Without this rule, LiteLLM would silently route bare names like
`claude-sonnet-4-5` to Anthropic-direct, breaking billing dashboards,
rate limits, and observability against the operator's intent.

### `:online` web-grounded analysis

Append `:online` to any model string to enable OpenRouter's web search
step before the completion. Caretaker's shipped defaults use this for
`upgrade_impact_analysis`, `migration_analysis`, and `migration_plan`,
where release notes and breaking-change advisories benefit from current
external context.

**Cost note:** `:online` adds approximately $4 per 1k searches on top
of the model call. The OTel span attribute `caretaker.llm.online=true`
lets cost dashboards filter web-grounded spend without parsing model
strings downstream.

### Browse OpenRouter's model catalogue

See [openrouter.ai/models](https://openrouter.ai/models) for the
current model list. Model IDs occasionally rename; if Caretaker's
shipped defaults stop resolving, override them in `feature_models`.

### Deployment note (k8s)

For Caretaker deployments running on Kubernetes (e.g. the Azure setup
referenced from this project), the `OPENROUTER_API_KEY` must be
available in the runtime environment. The recommended pattern is a
`SealedSecret` sourced from your local environment and committed to
your manifests repository before merging the Caretaker code change
that depends on the key.
