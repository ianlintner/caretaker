# Caretaker

Caretaker is an autonomous repository-maintenance system that uses GitHub Copilot as the execution engine and a Python orchestrator as the coordinator.

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
- [Configuration reference](configuration.md)
- [Agent overview](agents.md)
- [Development workflow](development.md)

## Repository pointers

- setup guide: [`dist/SETUP_AGENT.md`](https://github.com/ianlintner/caretaker/blob/main/dist/SETUP_AGENT.md)
- default config template: [`dist/templates/config-default.yml`](https://github.com/ianlintner/caretaker/blob/main/dist/templates/config-default.yml)
- schema: [`schema/config.v1.schema.json`](https://github.com/ianlintner/caretaker/blob/main/schema/config.v1.schema.json)
- source package: [`src/caretaker`](https://github.com/ianlintner/caretaker/tree/main/src/caretaker)

## Dogfooding

This repository is also the central caretaker repository, which means it uses its own automation to maintain itself. Very on-brand. Slightly terrifying. Mostly useful.
