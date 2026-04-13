# Changelog

All notable changes to this project will be documented in this file.

## [0.1.0] - Unreleased

### Added

- Initial project skeleton with Python 3.12 / hatchling build
- GitHub REST API async client (httpx-based)
- PR Agent: CI monitoring, failure triage, Copilot fix requests, auto-merge
- Issue Agent: classification, auto-dispatch, escalation
- Upgrade Agent: release checking, version pinning, upgrade issue creation
- Orchestrator: coordinates all agents, state persistence via GitHub issues
- LLM layer: Copilot structured comment protocol, optional Claude integration
- State tracking: issue-backed persistence with run summaries
- CLI entrypoint (`project-maintainer run`)
- Consumer templates: workflow, agent files, config, copilot instructions
- SETUP_AGENT.md: zero-config onboarding via Copilot
- CI pipeline: ruff lint, mypy strict, pytest with coverage
- Test suite: PR state machine, CI triage, review analysis, merge policy, models, config
