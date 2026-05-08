# Caretaker

Caretaker is an autonomous repository-maintenance system. A Python control plane on AKS receives GitHub App webhooks, decides what should happen next, and dispatches code changes to one of four coding backends — GitHub Copilot, an in-process LLM tool loop (Foundry), the `opencode_local` / `claude-code-action` hand-off, or a per-task Kubernetes Job.

It watches pull requests, issues, CI failures, upgrades, stale work, and security findings, then routes the right next action instead of waiting for a human to play inbox pinball.

## What it does

- triages pull requests and CI failures
- classifies and dispatches issues
- opens upgrade issues when newer caretaker releases exist
- raises dependency and security findings for remediation
- produces documentation and escalation digests
- tracks state across runs using GitHub itself

## Start here

- [Getting started](getting-started.md)
- [Architecture](architecture.md) — the runtime topology, webhook event pipeline, coding-job lifecycle, and 18-agent inventory
- [Configuration reference](configuration.md)
- [Agent overview](agents.md)
- [Development workflow](development.md)

## Repository pointers

- setup guide: [`setup-templates/SETUP_AGENT.md`](https://github.com/ianlintner/caretaker/blob/main/setup-templates/SETUP_AGENT.md)
- default config template: [`setup-templates/templates/config-default.yml`](https://github.com/ianlintner/caretaker/blob/main/setup-templates/templates/config-default.yml)
- schema: [`schema/config.v1.schema.json`](https://github.com/ianlintner/caretaker/blob/main/schema/config.v1.schema.json)
- source package: [`src/caretaker`](https://github.com/ianlintner/caretaker/tree/main/src/caretaker)

## Dogfooding

This repository is also the central caretaker repository, which means it uses its own automation to maintain itself. Very on-brand. Slightly terrifying. Mostly useful.
