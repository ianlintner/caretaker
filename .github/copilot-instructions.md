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

### Skills System

Caretaker provides **skills** to help you work effectively:

#### Available Skills (in `.github/skills/`)

- **caretaker-setup**: Set up caretaker in a repository
- **caretaker-config**: Configure and customize caretaker behavior
- **caretaker-debug**: Debug caretaker issues and failures
- **caretaker-agent-dev**: Develop custom caretaker agents
- **caretaker-upgrade**: Upgrade caretaker versions

#### Using Skills

When working with caretaker:
1. Reference relevant skills for guidance
2. Follow patterns and examples in skills
3. Use skills to understand caretaker conventions
4. Consult skills for troubleshooting

Example: If debugging a workflow failure, consult `.github/skills/caretaker-debug.md` for systematic diagnosis steps.

### Agent Personas

Agent persona files in `.github/agents/` define specialized roles:

- **maintainer-pr.md**: Fix PRs and handle CI failures
- **maintainer-issue.md**: Implement features and fix bugs from issues
- **maintainer-upgrade.md**: Handle caretaker version upgrades
- **karpathy-guidelines.md**: General coding best practices
- **claude-code-guidelines.md**: Claude Code specific patterns

When assigned work by caretaker, reference the appropriate agent file for:
- Communication protocols (task/result format)
- Capabilities and constraints
- Workflows and procedures
- Error handling guidance

### Quality Standards

Before committing code:
- Syntax is valid (YAML for config, proper Python/JS/etc.)
- Tests pass (run relevant test suite)
- Linting is clean (ruff/eslint/etc.)
- Documentation is updated if needed
- Changes are minimal and focused

### Working with Other Agents

Caretaker uses multiple agents that may coordinate:
- Issue Agent triages issues and assigns work
- PR Agent monitors PRs and requests fixes
- Security Agent handles vulnerabilities
- Docs Agent maintains documentation

When one agent finishes, it may pass work to another. Follow structured communication formats for clear handoffs.
