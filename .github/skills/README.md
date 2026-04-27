# Caretaker Skills

This directory contains **skills** for developers working with caretaker. Skills are reusable, focused capabilities that help both human developers (using Claude Code or GitHub Copilot) and AI coding agents understand and work with caretaker effectively.

## What are Skills?

Skills are specialized knowledge modules that provide:
- Clear capability descriptions
- Step-by-step guidance
- Working examples
- Best practices
- Troubleshooting help

## Available Skills

### Core Skills

1. **[caretaker-setup](./caretaker-setup.md)** - Set up caretaker in a repository
   - Analyze repo structure
   - Generate configuration
   - Create workflow files
   - Configure agent personas

2. **[caretaker-config](./caretaker-config.md)** - Configure and customize caretaker
   - Understand configuration options
   - Generate config snippets
   - Validate configurations
   - Tune for specific needs

3. **[caretaker-debug](./caretaker-debug.md)** - Debug caretaker issues
   - Analyze run logs
   - Check GitHub Actions setup
   - Diagnose agent failures
   - Fix common problems

4. **[caretaker-agent-dev](./caretaker-agent-dev.md)** - Develop custom agents
   - Understand agent protocol
   - Implement new agents
   - Test agent behavior
   - Register and deploy

5. **[caretaker-upgrade](./caretaker-upgrade.md)** - Upgrade caretaker versions
   - Check for updates
   - Analyze breaking changes
   - Plan migration
   - Execute upgrade

6. **[caretaker-qa-cycle](./caretaker-qa-cycle.md)** - Run a live-fire QA cycle against caretaker-qa
   - Decide whether a release warrants a live-fire QA cycle
   - Author scenario issues that exercise specific behaviors
   - Fast-forward the testbed pin to the release under test
   - Watch a scheduled run and assert against documented invariants
   - Triage findings and ship the fix

## How to Use Skills

### For Human Developers

#### Using Claude Code
When working with caretaker in Claude Code, you can reference skills:

```
"I want to set up caretaker in my repository"
```

Claude Code will:
1. Detect the caretaker-setup skill
2. Follow the skill's guidance
3. Execute the setup steps
4. Verify everything works

#### Using GitHub Copilot
Skills inform Copilot's behavior through the patterns and examples they contain. Copilot will:
1. Understand caretaker conventions
2. Generate appropriate code/config
3. Follow best practices
4. Provide helpful suggestions

### For AI Coding Agents

When caretaker agents (PR agent, issue agent, etc.) work with code, they can reference skills to:
- Understand how to perform complex operations
- Follow established patterns
- Avoid common mistakes
- Provide consistent behavior

## Skill Structure

Each skill follows a consistent structure:

```markdown
# Skill: {name}

## Purpose
One-sentence description of what this skill does

## Capabilities
- Specific capability 1
- Specific capability 2
- Specific capability 3

## When to Use
- Scenario 1 when this skill is helpful
- Scenario 2 when this skill is helpful

## Usage Examples
Detailed walkthroughs with concrete examples

## Implementation Guide
Step-by-step instructions

## Common Patterns
Reusable code/config patterns

## Troubleshooting
Common issues and their solutions

## Related Skills
Links to related skills
```

## Creating New Skills

To create a new skill:

1. Copy an existing skill as a template
2. Follow the structure above
3. Provide concrete, tested examples
4. Include troubleshooting section
5. Add to this README
6. Test with Claude Code and Copilot

See [docs/skills/skill-development-guide.md](../../docs/skills/skill-development-guide.md) for detailed guidance.

## Skill Development Principles

1. **Focused**: Each skill should do one thing well
2. **Concrete**: Provide specific, actionable guidance
3. **Tested**: All examples should be verified to work
4. **Maintained**: Keep skills up-to-date with caretaker changes
5. **Discoverable**: Clear names and descriptions

## Related Resources

- **[Agent Files](../agents/)** - Agent personas for AI execution
- **[Configuration Docs](../../docs/configuration.md)** - Detailed config reference
- **[Architecture Plan](../../plan.md)** - Overall system architecture
- **[Skills Plan](../../docs/skills-and-agents-plan.md)** - Skills enhancement roadmap

## Contributing

We welcome contributions of new skills! Please:

1. Read the skill development guide
2. Open an issue to discuss the proposed skill
3. Create the skill following the template
4. Test thoroughly
5. Submit a PR with documentation

## Support

- **Issues**: Report problems at [GitHub Issues](https://github.com/ianlintner/caretaker/issues)
- **Discussions**: Ask questions at [GitHub Discussions](https://github.com/ianlintner/caretaker/discussions)
- **Documentation**: Read the full docs at [docs/](../../docs/)

## License

Skills are part of the caretaker project and follow the same license.
