# Upgrade Guide

This guide describes how to upgrade `caretaker` in consumer repositories.

## Non-breaking upgrades

Most releases are non-breaking and can be automated by the Upgrade Agent.

1. The orchestrator detects a new release in `releases.json`.
2. It opens a maintainer upgrade issue assigned to `@copilot`.
3. Copilot updates `.github/maintainer/.version` and related templates.
4. CI validates the upgrade and the PR can be merged.

## Breaking upgrades

Breaking releases are flagged with `breaking: true` in `releases.json` and require manual review.

Typical migration actions:

- Move or rename config keys according to release notes.
- Refresh agent templates under `.github/agents/`.
- Update appended instructions in `.github/copilot-instructions.md`.
- Verify CI and behavior before merging.

## Rollback

If an upgrade introduces regressions:

1. Revert the upgrade PR.
2. Restore previous `.github/maintainer/.version`.
3. Re-run workflow with `workflow_dispatch` in `dry-run` or `upgrade-only` mode to inspect state.

## Config schema

`caretaker` currently supports config schema version `v1`.

- Validation is strict (unknown fields are rejected).
- Use the CLI to validate config locally:
  - `caretaker validate-config --config .github/maintainer/config.yml`

## BYOCA — opencode and the pluggable coding-agent registry

`caretaker.claude_code_executor.ClaudeCodeExecutor` and the closed
`executor.provider` enum (`copilot | foundry | claude_code | auto`) are
now backed by a registry of pluggable coding agents. Behaviour is
backward-compatible: existing configs keep working byte-identically.

To opt in to opencode as a peer of Claude Code, add an `executor.opencode`
block to `.github/maintainer/config.yml`:

```yaml
executor:
  provider: opencode      # was: claude_code (or copilot/foundry)
  claude_code:
    enabled: true         # leave on if you want both available
  opencode:
    enabled: true
    trigger_label: opencode
    mention: "@opencode-agent"
    max_attempts: 2

pr_reviewer:
  complex_reviewer: opencode   # default: claude_code
```

You will also need the opencode workflow files in your repo:

- `.github/workflows/opencode.yml`
- `.github/workflows/opencode-review.yml`

The maintainer agent's sync issue lists both alongside the existing
Claude templates in an "Optional templates" section. Copy them only when
the matching feature is enabled.

### Per-PR overrides

`agent:opencode` and any other `agent:<registered-name>` label now
forces routing to that specific agent. The legacy `agent:custom`,
`agent:copilot`, and `agent:quarantine` labels keep working unchanged.

### Deprecations

These names continue to work for one release and will be removed after:

- `caretaker.claude_code_executor.ClaudeCodeExecutor` →
  `caretaker.coding_agents.ClaudeCodeAgent`
- `caretaker.foundry.dispatcher.RouteOutcome.CLAUDE_CODE` →
  `RouteOutcome.CUSTOM_AGENT` plus `RouteResult.agent_name == "claude_code"`
- `ExecutorDispatcher(claude_code_executor=...)` constructor argument
  → `ExecutorDispatcher(registry=...)`
