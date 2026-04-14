# Agents

Caretaker is organized as a set of focused agents coordinated by the orchestrator.

## Core agents

| Agent            | Responsibility                                                                |
| ---------------- | ----------------------------------------------------------------------------- |
| PR agent         | monitors pull requests, triages CI failures, requests fixes, merges when safe |
| Issue agent      | classifies issues and dispatches work to Copilot or escalates it              |
| Upgrade agent    | detects new caretaker releases and opens upgrade work                         |
| DevOps agent     | turns default-branch CI failures into actionable fix issues                   |
| Self-heal agent  | investigates caretaker's own workflow failures                                |
| Security agent   | triages Dependabot, code scanning, and secret scanning alerts                 |
| Dependency agent | reviews Dependabot PRs, auto-merges safe bumps, posts digests                 |
| Docs agent       | reconciles merged PRs into changelog/docs updates                             |
| Charlie agent    | closes duplicate or abandoned caretaker-managed issues and PRs                |
| Stale agent      | warns/closes stale work and prunes merged branches                            |
| Escalation agent | creates a digest for work requiring human attention                           |

## How they collaborate

- the **orchestrator** decides which agent to run based on the event or scheduled mode
- the **GitHub client** is the shared integration layer for repo state and mutations
- the **state tracker** persists issue/PR tracking data in GitHub comments
- the **LLM layer** adds higher-quality reasoning where configured
- the **Charlie agent** handles short-horizon cleanup for caretaker-managed operational clutter

## Event mapping

| GitHub signal                                                     | Typical agent path                                 |
| ----------------------------------------------------------------- | -------------------------------------------------- |
| `pull_request`, `pull_request_review`, `check_run`, `check_suite` | PR agent                                           |
| `issues`, `issue_comment`                                         | Issue agent                                        |
| `workflow_run`                                                    | DevOps agent + Self-heal agent                     |
| `repository_vulnerability_alert`                                  | Security agent                                     |
| scheduled/manual full run                                         | orchestrator invokes the broader maintenance cycle |

## Copilot-facing instructions

The repo also ships instruction files for Copilot-driven execution:

- `.github/copilot-instructions.md`
- `.github/agents/maintainer-pr.md`
- `.github/agents/maintainer-issue.md`
- `.github/agents/maintainer-upgrade.md`
- `.github/agents/devops-build-triage.md`
- `.github/agents/docs-update.md`
- `.github/agents/maintainer-self-heal.md`
- `.github/agents/dependency-upgrade.md`
- `.github/agents/security-triage.md`
- `.github/agents/escalation-review.md`

Those files define how Copilot should behave when Caretaker assigns work or requests changes.
