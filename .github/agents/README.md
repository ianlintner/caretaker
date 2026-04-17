# Caretaker Agent Files

This directory contains **agent persona files** that guide AI coding agents (Copilot, Claude Code) when working with caretaker-managed repositories.

## What are Agent Files?

Agent files define specialized personas for different maintenance tasks. Each file specifies:
- Agent capabilities and constraints
- Communication protocols
- Task formats and response expectations
- Workflows and error handling
- Examples and troubleshooting guidance

## Core Agent Personas

### Maintenance Agents

1. **[maintainer-pr.md](./maintainer-pr.md)** - PR Maintenance Agent
   - Fix failing CI builds
   - Address code review comments
   - Resolve merge conflicts
   - Apply targeted fixes

2. **[maintainer-issue.md](./maintainer-issue.md)** - Issue Execution Agent
   - Implement features from issues
   - Fix reported bugs
   - Follow structured assignments
   - Create PRs for solutions

3. **[maintainer-upgrade.md](./maintainer-upgrade.md)** - Upgrade Agent
   - Upgrade caretaker versions
   - Handle breaking changes
   - Migrate configurations
   - Validate upgrades

### Specialized Agents

4. **[dependency-upgrade.md](./dependency-upgrade.md)** - Dependency Agent
   - Manage dependency updates
   - Handle Dependabot PRs
   - Resolve dependency conflicts
   - Test compatibility

5. **[docs-update.md](./docs-update.md)** - Documentation Agent
   - Update changelogs
   - Sync docs with code
   - Generate release notes
   - Maintain examples

6. **[security-triage.md](./security-triage.md)** - Security Agent
   - Triage security alerts
   - Apply security patches
   - Fix vulnerabilities
   - Validate fixes

7. **[devops-build-triage.md](./devops-build-triage.md)** - DevOps Agent
   - Diagnose CI failures
   - Fix build issues
   - Handle flaky tests
   - Optimize workflows

8. **[maintainer-self-heal.md](./maintainer-self-heal.md)** - Self-Heal Agent
   - Detect caretaker issues
   - Fix internal problems
   - Report upstream bugs
   - Self-diagnose

9. **[escalation-review.md](./escalation-review.md)** - Escalation Agent
   - Review blocked work
   - Create escalation digests
   - Prioritize human attention
   - Track resolution

## Behavioral Guidelines

### [karpathy-guidelines.md](./karpathy-guidelines.md)
General LLM coding best practices:
- Think before coding
- Simplicity first
- Surgical changes
- Goal-driven execution

### [claude-code-guidelines.md](./claude-code-guidelines.md)
Claude Code specific patterns:
- Tool usage optimization
- Context management
- Skill referencing
- Parallel operations
- Caretaker conventions

## How Agents Use These Files

### For GitHub Copilot

1. **Project Context**: Files inform Copilot about caretaker patterns
2. **Task Execution**: Copilot follows agent protocols when assigned work
3. **Quality Standards**: Files define expected behavior and output
4. **Error Handling**: Guidelines for when things go wrong

### For Claude Code

1. **Role Understanding**: Files define agent capabilities
2. **Protocol Compliance**: Structured communication formats
3. **Pattern Recognition**: Common scenarios and solutions
4. **Skill Integration**: How to use caretaker skills

### For Custom AI Agents

1. **Interface Definition**: Clear input/output contracts
2. **Behavior Specification**: What the agent should/shouldn't do
3. **Integration Guide**: How to interact with caretaker system
4. **Extension Points**: How to add new capabilities

## Agent Communication Protocol

### Task Format (Orchestrator → Agent)

```markdown
<!-- caretaker:task -->
TASK: {task description}
TYPE: {task classification}
PRIORITY: {low|medium|high}
ATTEMPT: {N} of {max}

**Context:**
{relevant background}

**Requirements:**
- [ ] Requirement 1
- [ ] Requirement 2

**Success Criteria:**
{how to verify completion}
<!-- /caretaker:task -->
```

### Response Format (Agent → Orchestrator)

```markdown
<!-- caretaker:result -->
RESULT: {SUCCESS|PARTIAL|BLOCKED|FAILED}
CHANGES: {summary of changes}
COMMITS: {commit hashes}
TESTS: {test results}
NOTES: {additional information}
<!-- /caretaker:result -->
```

## Creating Custom Agent Files

To create a new agent persona:

1. **Choose a Focus**: Pick a specific maintenance task
2. **Define Capabilities**: What can this agent do?
3. **Set Constraints**: What should it NOT do?
4. **Specify Protocol**: How does it communicate?
5. **Provide Examples**: Show concrete scenarios
6. **Add Troubleshooting**: Common issues and fixes

### Agent File Template

```markdown
# {Agent Name}

## Identity
You are a {role} for this repository.

## Core Capabilities
- Capability 1
- Capability 2

## Operating Constraints
- Constraint 1
- Constraint 2

## Communication Protocol
{input and output formats}

## Workflows
{step-by-step processes}

## Tools and Utilities
{available tools and when to use them}

## Success Metrics
{how to measure completion}

## Examples
{concrete scenarios}

## Troubleshooting
{common issues and solutions}

## Related Agents
{other agents to coordinate with}
```

See [../skills/caretaker-agent-dev.md](../skills/caretaker-agent-dev.md) for detailed guidance on agent development.

## Agent Coordination

Agents often work together:

```
Issue Agent
  ↓ triages and assigns
Security Agent
  ↓ analyzes vulnerabilities
PR Agent
  ↓ creates fix PR
DevOps Agent
  ↓ verifies CI passes
Docs Agent
  ↓ updates documentation
Review Agent
  ↓ validates everything
```

Each agent:
- Has clear responsibilities
- Communicates via structured formats
- Passes work to appropriate next agent
- Escalates when blocked

## Best Practices

### For Agent File Authors

1. **Be Specific**: Clear, actionable guidance
2. **Provide Examples**: Show don't just tell
3. **Define Boundaries**: What's in/out of scope
4. **Enable Autonomy**: Give enough info for independent work
5. **Plan for Errors**: How to handle failures

### For Agent Users (Copilot/Claude)

1. **Read Completely**: Understand full agent role
2. **Follow Protocols**: Use structured formats
3. **Stay in Scope**: Don't exceed agent capabilities
4. **Report Clearly**: Structured, parseable responses
5. **Escalate When Blocked**: Don't spin indefinitely

### For Repository Owners

1. **Customize**: Adapt agents to repo needs
2. **Document Changes**: Track modifications
3. **Test Behavior**: Verify agents work as expected
4. **Iterate**: Improve based on actual usage
5. **Share Learnings**: Contribute improvements back

## Testing Agent Behavior

```python
# Test agent understanding
1. Give agent a task from its persona file
2. Verify it follows the protocol
3. Check output format is correct
4. Validate it stays within constraints
5. Confirm error handling works
```

## Versioning and Updates

Agent files evolve with caretaker:
- **Minor updates**: Clarifications, examples
- **Major updates**: Protocol changes, new capabilities
- **Breaking changes**: Usually require caretaker upgrade

Track changes in each file's "Version History" section.

## Related Resources

- **[Skills](../skills/)** - Reusable knowledge modules
- **[Copilot Instructions](../copilot-instructions.md)** - Project-wide context
- **[Configuration](../../docs/configuration.md)** - System configuration
- **[Architecture](../../plan.md)** - Overall system design

## Contributing

We welcome improvements to agent files:

1. Use the agent and note pain points
2. Identify unclear or missing guidance
3. Propose specific improvements
4. Test changes with real agents
5. Submit PR with updated file(s)

## Support

- **Issues**: Report problems at [GitHub Issues](https://github.com/ianlintner/caretaker/issues)
- **Discussions**: Ask questions at [GitHub Discussions](https://github.com/ianlintner/caretaker/discussions)
- **Documentation**: Full docs at [docs/](../../docs/)

## License

Agent files are part of the caretaker project and follow the same license.

---

**Note**: These agent files are designed to work with both GitHub Copilot and Claude Code. They use GitHub-native integration points (Copilot) and can be referenced as skills (Claude Code).
