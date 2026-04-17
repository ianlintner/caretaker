# Claude Code Guidelines for Caretaker

Best practices for working with caretaker using Claude Code. These guidelines complement the general [Karpathy guidelines](./karpathy-guidelines.md) with Claude Code-specific patterns.

## Core Principles

1. **Use Skills Proactively**: Reference caretaker skills in `.github/skills/` for guidance
2. **Leverage Tool Parallelism**: Call multiple independent tools simultaneously
3. **Manage Context Efficiently**: Use Task tool for exploration, direct tools for known operations
4. **Structure for Clarity**: Use clear sections and formatting in responses
5. **Verify Before Committing**: Always validate changes before pushing

## Tool Usage Patterns

### When to Use Task Tool

Use the Task tool with appropriate subagent for:
- **Exploration**: "Find all files that handle authentication" → Use Explore agent
- **Multi-file search**: When you don't know exact file locations
- **Complex analysis**: Require multiple rounds of investigation
- **Planning**: Need to design implementation approach

**Don't** use Task tool for:
- Reading specific known files
- Simple grep/glob searches
- Single-file operations
- Quick lookups

### When to Use Direct Tools

Use Read, Grep, Glob directly for:
- **Known file paths**: You know exactly what to read
- **Simple searches**: Pattern matching in known locations
- **Quick validation**: Check a specific condition
- **Single operations**: No dependencies or exploration needed

### Tool Parallelism

**Always parallelize** independent operations:

```typescript
// Good - Parallel reads
Read("src/agent.py")
Read("tests/test_agent.py")
Read(".github/maintainer/config.yml")

// Bad - Sequential reads
Read("src/agent.py") → wait → Read("tests/test_agent.py") → wait → ...
```

**Never parallelize** dependent operations:

```typescript
// Good - Sequential when dependent
Bash("npm test") → wait → Read("test-results.json") → analyze

// Bad - Parallel when dependent
Bash("npm test") + Read("test-results.json")  // File doesn't exist yet!
```

## Caretaker-Specific Patterns

### Pattern 1: Analyze Configuration

```python
# Good approach
1. Read(".github/maintainer/config.yml")  # Get config
2. If needed, check schema: Read("schema/config.v1.schema.json")
3. Validate configuration logic
4. Suggest improvements

# With skills reference
"I'll use the caretaker-config skill to help analyze this configuration..."
```

### Pattern 2: Debug Workflow Failure

```python
# Good approach
1. Use caretaker-debug skill for systematic diagnosis
2. Read workflow file: Read(".github/workflows/maintainer.yml")
3. Parallel check:
   - Read config: Read(".github/maintainer/config.yml")
   - Read version: Read(".github/maintainer/.version")
   - Check agent files: Glob(".github/agents/*.md")
4. Identify issue from patterns
5. Propose specific fix
6. Verify fix will work

# Reference skill
"Let me use the caretaker-debug skill to diagnose this issue systematically..."
```

### Pattern 3: Implement New Agent

```python
# Good approach
1. Reference caretaker-agent-dev skill
2. Explore existing agents: Task(Explore, "How are agents structured?")
3. Read agent protocol: Read("src/caretaker/agent_protocol.py")
4. Read example agent: Read("src/caretaker/pr_agent/agent.py")
5. Design new agent following patterns
6. Implement with tests
7. Register in agents.py
8. Verify integration

# Use Plan Mode for complex implementations
EnterPlanMode() → design → ExitPlanMode() → implement
```

### Pattern 4: Update Documentation

```python
# Good approach
1. Read current docs: Read("docs/configuration.md")
2. Check for related docs: Glob("docs/**/*.md")
3. Identify what needs updating
4. Make targeted changes with Edit tool
5. Verify consistency across docs
6. Check examples still work

# Parallel reads for efficiency
Read("docs/configuration.md") + Read("README.md") + Read("docs/architecture.md")
```

## Context Management

### Skill References Save Context

Instead of explaining caretaker concepts repeatedly, reference skills:

```markdown
// Verbose (wastes context)
"Caretaker uses a config file at .github/maintainer/config.yml.
This file has sections for each agent. The pr_agent section controls
PR behavior. You can set auto_merge to true to enable..."

// Concise (saves context)
"See the caretaker-config skill for configuration details. Key setting:
pr_agent.auto_merge controls PR auto-merging behavior."
```

### Use Task Tool for Heavy Exploration

When exploring unfamiliar code:

```python
# Good - Delegate to Explore agent
Task(Explore, "How does the PR agent handle CI failures?")

# Bad - Many manual greps (wastes context and time)
Grep("ci_failure") + Grep("handle_ci") + Grep("check_ci") + ...
```

### Structure Responses Clearly

Use clear sections to help users and maintain context:

```markdown
## Analysis
{what you found}

## Proposed Solution
{what you'll do}

## Implementation
{code changes}

## Verification
{how to verify it works}
```

## Error Handling

### Graceful Degradation

```python
# Good - Handle tool failures gracefully
try:
    result = Read("optional_file.py")
except FileNotFoundError:
    # Continue with alternative approach
    "File not found, proceeding with default configuration..."

# Provide helpful context
"I couldn't find X, which might mean Y. Let me check Z instead..."
```

### Clear Error Messages

```python
# Good - Specific and actionable
"The configuration file has a syntax error on line 23:
'merge_method' must be one of: squash, merge, or rebase.
Current value 'fast-forward' is invalid."

# Bad - Vague
"Configuration error detected."
```

## Testing and Validation

### Always Validate Before Committing

```python
# Required validation steps
1. Syntax check: yamllint for YAML, ruff for Python
2. Type check: mypy for Python code
3. Unit tests: pytest for new code
4. Integration test: Dry-run if possible
5. Document changes: Update relevant docs

# Validation workflow
Edit(...) → validate syntax → check types → run tests → commit
```

### Use Todo List for Complex Tasks

```python
# Good - Track progress
TodoWrite([
  {content: "Analyze configuration", status: "completed"},
  {content: "Design solution", status: "completed"},
  {content: "Implement changes", status: "in_progress"},
  {content: "Write tests", status: "pending"},
  {content: "Update docs", status: "pending"},
  {content: "Run CI validation", status: "pending"}
])
```

## Caretaker Repository Conventions

### Branch Naming

```bash
# For maintainer work
maintainer/{type}-{description}

# Examples
maintainer/feat-new-agent
maintainer/fix-ci-timeout
maintainer/docs-update-config
```

### Commit Messages

```bash
# Format
{type}({scope}): {description}

# Examples
feat(agents): add code review agent
fix(pr-agent): handle null CI status
docs(skills): add caretaker-config skill
chore(deps): update dependencies
```

### Pre-Commit Validation

Always run before committing to caretaker:

```bash
ruff check src/ tests/
ruff format --check src/ tests/
mypy src/
pytest tests/ -q
# If docs changed: mkdocs build --strict
```

## Collaboration with Other Agents

### Handoffs to Copilot

When creating tasks for Copilot:

```markdown
# Good - Structured and clear
@copilot Please implement this fix.

<!-- caretaker:task -->
TASK: Fix CI failure
TYPE: TEST_FAILURE
PRIORITY: high

**Context:**
{relevant background}

**Requirements:**
- [ ] Fix the failing test
- [ ] Ensure all tests pass
- [ ] No breaking changes

**Success Criteria:**
All CI checks passing
<!-- /caretaker:task -->

# Reference agent file
See .github/agents/maintainer-pr.md for your workflow.
```

### Reviewing Copilot Work

When Copilot completes work:

```python
1. Read Copilot's changes
2. Verify meets requirements
3. Check test coverage
4. Validate no breaking changes
5. Approve or request changes with specific feedback
```

## Advanced Patterns

### Multi-Agent Coordination

```python
# When multiple agents should work together
1. Issue agent triages → assigns to specialized agent
2. Specialized agent completes → notifies review agent
3. Review agent validates → approves or escalates
4. Documentation agent updates docs

# Each agent references its persona file
"Following .github/agents/code-review-agent.md protocol..."
```

### Skill Composition

```python
# Combine multiple skills for complex tasks
1. Use caretaker-setup for initial configuration
2. Use caretaker-config to tune settings
3. Use caretaker-debug to verify everything works
4. Use caretaker-agent-dev to add custom behavior
```

## Common Mistakes to Avoid

### ❌ Over-Using Task Tool

```python
# Bad - Task tool for simple read
Task(Explore, "Read src/config.py")

# Good - Direct read
Read("src/config.py")
```

### ❌ Sequential Independent Operations

```python
# Bad - Slow sequential reads
Read("file1.py") → wait → Read("file2.py") → wait

# Good - Fast parallel reads
Read("file1.py") + Read("file2.py")
```

### ❌ Not Referencing Skills

```python
# Bad - Reinventing guidance
"Let me explain how to configure caretaker..."

# Good - Reference existing skill
"See caretaker-config skill for configuration guidance."
```

### ❌ Vague Change Descriptions

```python
# Bad
"I updated the config file."

# Good
"Updated .github/maintainer/config.yml:
- Changed schedule from weekly to daily
- Enabled pr_agent auto_merge for copilot_prs
- Added ci_log_analysis to Claude features"
```

### ❌ Skipping Validation

```python
# Bad - Commit without testing
Edit(...) → Commit

# Good - Validate first
Edit(...) → Bash("ruff check") → Bash("pytest") → Commit
```

## Quick Reference

### Tool Selection Matrix

| Task | Tool | Subagent/Pattern |
|------|------|------------------|
| Explore codebase | Task | Explore agent |
| Read known file | Read | Direct path |
| Search for pattern | Grep | With glob filter |
| Find files | Glob | Pattern matching |
| Complex planning | Task | Plan agent |
| Run commands | Bash | Sequential if dependent |
| Edit files | Edit | After Read |
| Write new files | Write | After Read if exists |

### Validation Checklist

- [ ] Syntax valid (YAML/Python)
- [ ] Types correct (mypy)
- [ ] Tests pass (pytest)
- [ ] Linting clean (ruff)
- [ ] Docs updated
- [ ] Commit message clear
- [ ] Changes are minimal

### When to Use Skills

- **caretaker-setup**: Initial repository setup
- **caretaker-config**: Configuration tuning
- **caretaker-debug**: Troubleshooting issues
- **caretaker-agent-dev**: Building new agents
- **caretaker-upgrade**: Version upgrades

## Summary

1. **Reference skills** instead of repeating context
2. **Parallelize** independent operations
3. **Use Task tool** for exploration, direct tools for known operations
4. **Structure** responses clearly
5. **Validate** before committing
6. **Follow conventions** for commits and branches
7. **Coordinate** effectively with other agents

These guidelines help you work efficiently with caretaker while maintaining high code quality and clear communication.

## Related Resources

- [Karpathy Guidelines](./karpathy-guidelines.md) - General LLM coding guidelines
- [Skills Directory](../skills/) - Available caretaker skills
- [Agent Files](../agents/) - Agent persona files
- [Architecture Plan](../../plan.md) - System architecture

## Version History

- v1.0 - Initial guidelines (2026-04-17)
