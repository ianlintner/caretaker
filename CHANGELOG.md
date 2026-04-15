# Changelog

All notable changes to this project will be documented in this file.

## [2026-W16] — 2026-04-15

- add Charlie agent for janitorial cleanup of caretaker-managed issues and PRs (#237)
- Followup (#238)
- handle 403 rate-limit errors and guard state load against unhandled crash (#244)
- docs build no longer fails on configure-pages API errors (#256)
- Remove committed site/ build artifacts; add CodeQL exclusion config (#259)
- guard FailureType → TaskType conversion against unmapped values (#263)
- treat 405/409/422 merge rejections as waiting, not errors (#265)
- [WIP] Fix CI failure on main for Analyze (javascript-typescript) (#268)
- replace dynamic CodeQL javascript-typescript scan with explicit Python-only workflow (#269)
- group related issues/PRs by workflow run_id (#272)
- Agentic (#274)
- prevent duplicate @copilot task comments from concurrent workflow runs (#276)
- Resolve CodeQL `Analyze (python)` failure by removing conflicting advanced workflow (#279)
- Remove conflicting advanced CodeQL workflow causing `Analyze (python)` failures on `main` (#283)
- Self-heal: avoid env-noise “unknown error” titles by extracting from full job log (#286)
- Improve self-heal unknown failure extraction to avoid environment-noise issue titles (#288)
- [WIP] Fix caretaker self-heal for unknown failure (#290)
- Route Copilot wake-up comments through COPILOT_PAT identity (#292)
- Self-heal: extract actionable unknown-failure messages from Actions logs (#293)
- Add sync issue builder for client workflow/file reconciliation (#295)
- [WIP] Add installation of Claude agent from improvement repo (#297)
- address agent/orchestrator missed-goal patterns from workflow analysis (#298)
- Handle mixed naive/aware datetimes in orchestrator reconciliation (#300)
- handle 422 "Reference already exists" gracefully in DocsAgent (#304)
- handle 422 branch-already-exists gracefully (#306)
- [WIP] Fix unknown caretaker failure with exit code 1 (#308)

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
- CLI entrypoint (`caretaker run`)
- Consumer templates: workflow, agent files, config, copilot instructions
- SETUP_AGENT.md: zero-config onboarding via Copilot
- CI pipeline: ruff lint, mypy strict, pytest with coverage
- Test suite: PR state machine, CI triage, review analysis, merge policy, models, config
