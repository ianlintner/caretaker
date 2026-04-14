# Configuration

Caretaker configuration is defined by the `MaintainerConfig` model in [`src/caretaker/config.py`](https://github.com/ianlintner/caretaker/blob/main/src/caretaker/config.py).

## Main files

- config file: `.github/maintainer/config.yml`
- JSON schema: [`schema/config.v1.schema.json`](https://github.com/ianlintner/caretaker/blob/main/schema/config.v1.schema.json)
- default template: [`dist/templates/config-default.yml`](https://github.com/ianlintner/caretaker/blob/main/dist/templates/config-default.yml)

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
| `upgrade_agent`    | caretaker release upgrade strategy                    |
| `devops_agent`     | CI failure detection on the default branch            |
| `self_heal_agent`  | caretaker-on-caretaker failure diagnosis              |
| `security_agent`   | Dependabot, code scanning, and secret scanning triage |
| `dependency_agent` | dependency PR merging and digest generation           |
| `docs_agent`       | changelog/docs reconciliation                         |
| `stale_agent`      | stale issues, PRs, and merged branch cleanup          |
| `human_escalation` | digest issue for work needing maintainer action       |
| `escalation`       | escalation targets and stale-age policy               |
| `llm`              | optional Claude feature toggles                       |

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

stale_agent:
  stale_days: 60
  close_after: 14
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
