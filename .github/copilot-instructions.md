<!-- Added by caretaker (dogfood) -->

## Caretaker System

This IS the central caretaker repository. It uses its own system for maintenance.

### How it works

- An orchestrator runs hourly via GitHub Actions
- It creates issues and assigns them to @copilot for execution
- When @copilot opens PRs, the orchestrator monitors them through CI, review, and merge
- The orchestrator communicates with @copilot via structured issue/PR comments

### When assigned an issue by caretaker

- Read the full issue body carefully — it contains structured instructions
- Follow the instructions exactly as written
- If unclear, comment on the issue asking for clarification
- Always ensure CI passes before considering work complete
- Reference the agent file for your role: `.github/agents/maintainer-pr.md` or `maintainer-issue.md`

### Pre-push checklist

Before pushing any commits, **always** run the full CI validation locally and confirm every step passes:

1. `ruff check src/ tests/` — lint
2. `ruff format --check src/ tests/` — format check
3. `mypy src/` — type check
4. `pytest tests/ -q` — tests
5. `mkdocs build --strict` — docs build (if docs changed)

If any step fails, fix it before committing/pushing. Do not push code that has not passed all checks.

### Conventions

- Branch naming: `maintainer/{type}-{description}`
- Commit messages: `chore(maintainer): {description}`
- Always run existing tests before pushing
- Do not modify `.github/maintainer/` files unless explicitly instructed

### Coding Guidelines

Follow the behavioral guidelines in `.github/agents/karpathy-guidelines.md` to reduce common LLM coding mistakes:
- Think before coding - state assumptions, surface tradeoffs
- Simplicity first - minimum code, no speculative features
- Surgical changes - touch only what's necessary
- Goal-driven execution - define success criteria and verify
