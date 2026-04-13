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
