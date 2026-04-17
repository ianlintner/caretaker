# Skills and Claude Agents Enhancement Plan

## Executive Summary

This plan outlines enhancements to the caretaker system to support **skills** for human developers using Claude Code and Copilot, along with **focused custom agent files** for specialized tasks. The goal is to improve the developer experience for both human coders and AI coding agents working with caretaker-managed repositories.

## Context

Based on analysis of the current caretaker architecture:

- **12 specialized agents** handle different maintenance aspects
- **Copilot-first execution** via persona files in `.github/agents/`
- **MCP (Model Context Protocol)** support partially implemented
- **Behavioral guidelines** exist but could be enhanced
- **No skill system** for human developers using Claude Code or Copilot

## Objectives

1. **Skills for Human Developers**: Create reusable skills that help developers working with Claude Code understand and work with caretaker
2. **Enhanced Agent Files**: Improve agent personas with best practices from Claude Code
3. **MCP Skill Integration**: Enable caretaker agents to use MCP skills
4. **Developer Experience**: Make it easier for both humans and AI agents to contribute

## 1. Skills for Human Developers

### 1.1 What are Skills?

In Claude Code, skills are reusable, focused capabilities that can be invoked to perform specific tasks. Skills are implemented as markdown files in the `.github/skills/` directory that define:
- Clear capability description
- Input/output contract
- Usage examples
- Implementation guidance

### 1.2 Proposed Skills for Caretaker

#### Skill: `caretaker-setup`
**Purpose**: Guide developers through initial caretaker setup in a repository

**Location**: `.github/skills/caretaker-setup.md`

**Capabilities**:
- Analyze repo structure (language, CI, branch protection)
- Generate appropriate `config.yml` with sensible defaults
- Create workflow file tailored to repo needs
- Set up agent persona files
- Configure Copilot instructions

**Target Users**: Repository owners, human developers setting up caretaker for the first time

#### Skill: `caretaker-agent-dev`
**Purpose**: Help developers create or modify caretaker agents

**Location**: `.github/skills/caretaker-agent-dev.md`

**Capabilities**:
- Guide through agent protocol implementation
- Explain state machine patterns
- Show how to register new agents
- Test agent behavior
- Debug agent issues

**Target Users**: Contributors to caretaker, developers extending caretaker

#### Skill: `caretaker-config`
**Purpose**: Help configure and customize caretaker behavior

**Location**: `.github/skills/caretaker-config.md`

**Capabilities**:
- Explain configuration options
- Validate config files
- Generate config snippets
- Troubleshoot config issues
- Show examples for common scenarios

**Target Users**: Repository owners customizing caretaker

#### Skill: `caretaker-debug`
**Purpose**: Debug caretaker issues in consumer repos

**Location**: `.github/skills/caretaker-debug.md`

**Capabilities**:
- Analyze caretaker run logs
- Check GitHub Actions setup
- Validate permissions and secrets
- Diagnose agent failures
- Suggest fixes for common problems

**Target Users**: All developers working with caretaker

#### Skill: `caretaker-upgrade`
**Purpose**: Guide users through caretaker version upgrades

**Location**: `.github/skills/caretaker-upgrade.md`

**Capabilities**:
- Check current vs latest version
- Analyze upgrade requirements
- Detect breaking changes
- Generate migration plan
- Validate post-upgrade setup

**Target Users**: Repository owners maintaining caretaker

### 1.3 Skill Implementation Pattern

Each skill follows this structure:

```markdown
# Skill: {skill-name}

## Purpose
Clear one-sentence description

## Capabilities
- What this skill can do
- Specific tasks it handles

## When to Use
- Scenario 1
- Scenario 2

## Usage Examples

### Example 1: {scenario}
{detailed walkthrough}

### Example 2: {scenario}
{detailed walkthrough}

## Implementation Guide

### For Claude Code
{specific instructions for Claude Code}

### For Copilot
{specific instructions for Copilot}

## Common Patterns
{reusable code/config patterns}

## Troubleshooting
{common issues and solutions}

## Related Skills
- {other-skill}
```

## 2. Enhanced Agent Files

### 2.1 Agent File Improvements

Current agent files are good but can be enhanced with:

1. **Clearer Structure**: Explicit sections for capabilities, constraints, protocols
2. **Better Examples**: Concrete before/after examples
3. **Error Handling**: Guidance on what to do when blocked
4. **Success Criteria**: Clear definition of task completion
5. **Context Management**: How to maintain context across interactions
6. **Tool Usage**: When to use which tools

### 2.2 New Specialized Agent Files

#### Agent: `code-review-agent`
**Purpose**: Automated code review for maintainer PRs

**Location**: `.github/agents/code-review-agent.md`

**Responsibilities**:
- Review code quality
- Check for security issues
- Verify tests are adequate
- Ensure documentation is updated
- Apply consistent style

#### Agent: `documentation-agent`
**Purpose**: Maintain documentation in sync with code

**Location**: `.github/agents/documentation-agent.md`

**Responsibilities**:
- Detect doc-code mismatches
- Update API documentation
- Generate changelog entries
- Keep README current
- Update examples

#### Agent: `test-generation-agent`
**Purpose**: Generate tests for uncovered code

**Location**: `.github/agents/test-generation-agent.md`

**Responsibilities**:
- Analyze test coverage
- Generate test cases
- Create test fixtures
- Add edge case tests
- Maintain test quality

#### Agent: `refactor-agent`
**Purpose**: Perform safe refactoring operations

**Location**: `.github/agents/refactor-agent.md`

**Responsibilities**:
- Identify refactoring opportunities
- Execute safe transformations
- Maintain backward compatibility
- Update all references
- Verify tests still pass

#### Agent: `performance-agent`
**Purpose**: Identify and fix performance issues

**Location**: `.github/agents/performance-agent.md`

**Responsibilities**:
- Profile code for bottlenecks
- Suggest optimizations
- Implement performance fixes
- Add performance tests
- Monitor regressions

### 2.3 Agent File Template

```markdown
# {Agent Name}

## Identity
You are a {role} for this repository. You are invoked by the caretaker orchestrator to {primary responsibility}.

## Core Capabilities
### What You Can Do
- Capability 1: {description}
- Capability 2: {description}
- Capability 3: {description}

### What You Should NOT Do
- Anti-pattern 1: {why not}
- Anti-pattern 2: {why not}

## Operating Constraints
### Technical Constraints
- File access: {what files you can modify}
- Git operations: {what git commands are allowed}
- External calls: {what APIs you can use}

### Policy Constraints
- Approval requirements: {when human approval needed}
- Retry limits: {how many attempts}
- Escalation triggers: {when to escalate}

## Communication Protocol
### Input Format
The orchestrator sends structured tasks:

```
<!-- caretaker:task -->
TASK: {task type}
TYPE: {classification}
PRIORITY: {level}
ATTEMPT: {N} of {max}

**Context:**
{relevant context}

**Requirements:**
- [ ] Requirement 1
- [ ] Requirement 2

**Success Criteria:**
{how to know when done}
<!-- /caretaker:task -->
```

### Output Format
Respond with structured results:

```
<!-- caretaker:result -->
RESULT: {SUCCESS|PARTIAL|BLOCKED|FAILED}
CHANGES: {summary of changes}
COMMITS: {commit hashes}
TESTS: {test results}
NOTES: {additional info}
<!-- /caretaker:result -->
```

## Workflows
### Standard Workflow
1. Read task completely
2. Analyze requirements
3. Plan approach
4. Execute changes
5. Verify success
6. Report results

### Error Recovery
If blocked or failed:
1. Document what you attempted
2. Explain the blocker
3. Suggest alternatives
4. Request clarification if needed

## Tools and Utilities
### Available Tools
- {tool 1}: {when to use}
- {tool 2}: {when to use}

### Tool Selection Guide
- For {scenario}: use {tool}
- For {scenario}: use {tool}

## Success Metrics
### Task Completion
- All requirements met: ✓
- Tests passing: ✓
- No breaking changes: ✓
- Documentation updated: ✓

### Quality Indicators
- Code follows repo conventions
- Changes are minimal and focused
- Commit messages are descriptive
- No security issues introduced

## Examples
### Example 1: {Common Scenario}
**Input:**
{example task}

**Process:**
1. {step}
2. {step}

**Output:**
{example result}

### Example 2: {Edge Case}
**Input:**
{example task}

**Process:**
1. {step}
2. {step}

**Output:**
{example result}

## Troubleshooting
### Common Issues
**Issue:** {problem}
**Solution:** {fix}

**Issue:** {problem}
**Solution:** {fix}

## Context Preservation
### What to Remember
- Current task state
- Previous attempts
- Known blockers
- Related issues/PRs

### What to Forget
- Completed tasks
- Unrelated context
- Stale information

## Related Agents
- {agent}: {relationship}
- {agent}: {relationship}
```

## 3. MCP Skill Integration

### 3.1 MCP Architecture for Caretaker

Building on the existing MCP backend (`src/caretaker/mcp_backend/`), enhance to support:

1. **Skill Discovery**: Register available MCP skills
2. **Skill Invocation**: Call skills from agents
3. **Skill Composition**: Chain multiple skills
4. **Skill Marketplace**: Share skills across repos

### 3.2 MCP Skill Protocol

```python
# Skill Definition
class CaretakerSkill:
    """Base class for caretaker MCP skills"""

    @property
    def name(self) -> str:
        """Unique skill identifier"""

    @property
    def description(self) -> str:
        """Human-readable description"""

    @property
    def capabilities(self) -> list[str]:
        """List of what this skill can do"""

    async def execute(
        self,
        context: SkillContext,
        params: dict
    ) -> SkillResult:
        """Execute the skill with given parameters"""
```

### 3.3 Proposed MCP Skills

1. **CI Analysis Skill**: Deep analysis of CI failures
2. **Code Search Skill**: Semantic code search across repos
3. **Dependency Analysis Skill**: Analyze dependency graphs
4. **Security Scan Skill**: Comprehensive security scanning
5. **Performance Profile Skill**: Profile code performance

### 3.4 Configuration

```yaml
# .github/maintainer/config.yml
mcp:
  enabled: true
  endpoint: "https://caretaker-mcp.azurewebsites.net"
  auth_mode: "managed_identity"

  skills:
    ci_analysis:
      enabled: true
      max_retries: 3

    code_search:
      enabled: true
      index_private: false

    dependency_analysis:
      enabled: true
      check_vulnerabilities: true

    security_scan:
      enabled: true
      severity_threshold: "medium"

    performance_profile:
      enabled: false  # opt-in
```

## 4. Enhanced Behavioral Guidelines

### 4.1 Current Guidelines Analysis

The existing `karpathy-guidelines.md` is excellent. Enhancements:

1. **Claude Code Specific**: Add Claude Code-specific patterns
2. **Copilot Specific**: Add Copilot-specific patterns
3. **Tool Selection**: Guidance on choosing the right tools
4. **Context Management**: How to manage long-running context
5. **Collaboration Patterns**: How agents should collaborate

### 4.2 New Guidelines Document

**Location**: `.github/agents/claude-code-guidelines.md`

Content:
- Tool usage best practices
- Context window management
- Parallel tool calling
- Error recovery patterns
- When to use Task tool vs direct execution
- How to structure responses for clarity

### 4.3 Enhanced Copilot Instructions

Update `.github/copilot-instructions.md` with:

1. **Skill Awareness**: How to discover and use skills
2. **Agent Protocols**: How to interact with caretaker agents
3. **MCP Integration**: When MCP skills are available
4. **Quality Standards**: Code quality expectations
5. **Testing Requirements**: When and how to write tests

## 5. Developer Experience Enhancements

### 5.1 For Human Developers Using Claude Code

1. **Quick Start Skill**: Get started with caretaker in minutes
2. **Interactive Guides**: Step-by-step workflows with verification
3. **Context-Aware Help**: Help that understands current state
4. **Error Explanations**: Clear explanations of errors
5. **Best Practices**: Inline suggestions for improvements

### 5.2 For Copilot

1. **Clearer Task Definitions**: More structured task descriptions
2. **Better Context**: Include relevant history and decisions
3. **Success Criteria**: Explicit completion conditions
4. **Feedback Loop**: Learn from past successes/failures
5. **Escalation Paths**: Clear escalation when blocked

### 5.3 For Claude Agents

1. **Agent Collaboration**: Protocols for multi-agent coordination
2. **State Sharing**: How agents share findings
3. **Task Handoffs**: Passing work between agents
4. **Conflict Resolution**: Handling conflicting recommendations
5. **Quality Gates**: Automated quality checks

## 6. Documentation Structure

```
.github/
  skills/                          # Skills for human developers
    caretaker-setup.md
    caretaker-agent-dev.md
    caretaker-config.md
    caretaker-debug.md
    caretaker-upgrade.md
    README.md                      # Skill directory index

  agents/                          # Agent personas
    maintainer-pr.md               # Existing
    maintainer-issue.md            # Existing
    maintainer-upgrade.md          # Existing
    code-review-agent.md           # New
    documentation-agent.md         # New
    test-generation-agent.md       # New
    refactor-agent.md              # New
    performance-agent.md           # New
    karpathy-guidelines.md         # Existing
    claude-code-guidelines.md      # New
    copilot-patterns.md            # New
    agent-collaboration.md         # New
    README.md                      # Agent directory index

  copilot-instructions.md          # Enhanced with skills

docs/
  skills/                          # Extended skill documentation
    skill-development-guide.md
    skill-testing.md
    skill-deployment.md

  agents/                          # Extended agent documentation
    agent-development-guide.md
    agent-testing.md
    agent-protocols.md
```

## 7. Implementation Phases

### Phase 1: Foundation (This PR)
**Goal**: Establish skill and agent infrastructure

**Deliverables**:
1. Skills directory structure
2. Initial 5 core skills for humans
3. Enhanced agent file template
4. Claude Code guidelines
5. Updated copilot instructions
6. Comprehensive documentation

**Timeline**: Current PR

### Phase 2: New Specialized Agents
**Goal**: Add specialized agent personas

**Deliverables**:
1. Code review agent
2. Documentation agent
3. Test generation agent
4. Agent collaboration protocols
5. Integration tests

**Timeline**: Follow-up PR

### Phase 3: MCP Skill Integration
**Goal**: Enable MCP skills in agents

**Deliverables**:
1. Enhanced MCP backend
2. Skill registry
3. 5 core MCP skills
4. Agent-to-skill protocols
5. End-to-end examples

**Timeline**: Separate PR (coordinated with Azure MCP work)

### Phase 4: Advanced Features
**Goal**: Polish and advanced capabilities

**Deliverables**:
1. Skill marketplace concept
2. Agent performance metrics
3. Advanced debugging tools
4. Multi-repo skill sharing
5. Community skill contributions

**Timeline**: Future iteration

## 8. Success Metrics

### For Skills
- **Discoverability**: Developers can find relevant skills in <30 seconds
- **Usability**: Skills reduce task completion time by >50%
- **Adoption**: Skills are used in >80% of caretaker interactions
- **Quality**: Skill-assisted tasks have <10% error rate

### For Agents
- **Autonomy**: Agents complete 90%+ of assigned tasks without escalation
- **Quality**: Agent-generated code passes review 85%+ of time
- **Speed**: Average task completion time reduced by 40%
- **Reliability**: Agent failure rate <5%

### For Developer Experience
- **Onboarding**: New users productive in <15 minutes
- **Satisfaction**: >90% positive feedback on tools
- **Efficiency**: Maintenance tasks take 60% less time
- **Confidence**: Developers trust agent decisions 95% of time

## 9. Examples and Patterns

### Example 1: Human Developer Using Setup Skill

```
Developer: "I want to set up caretaker in my Python project"

Claude Code (using caretaker-setup skill):
1. Analyzes repo structure
2. Detects pytest, ruff, mypy
3. Generates config.yml with Python-specific defaults
4. Creates workflow for hourly runs
5. Sets up PR and issue agents
6. Configures Copilot instructions
7. Opens PR with all changes

Result: One-step setup completed in 2 minutes
```

### Example 2: Agent Using MCP Skill

```
PR Agent detects CI failure:
1. Reads test failure logs
2. Calls MCP CI Analysis Skill
3. Skill performs deep analysis
4. Returns root cause + fix suggestion
5. PR Agent applies fix
6. Verifies tests pass
7. Reports success

Result: CI failure fixed autonomously in 3 minutes
```

### Example 3: Multi-Agent Collaboration

```
Issue: "Add new authentication method"

Issue Agent:
1. Classifies as FEATURE_MEDIUM
2. Decomposes into sub-tasks
3. Assigns to specialized agents

Security Agent:
- Reviews security implications
- Approves approach

Code Review Agent:
- Sets review criteria
- Monitors implementation

Test Generation Agent:
- Creates test plan
- Generates test cases

Refactor Agent:
- Identifies code that needs updating
- Plans refactor strategy

Documentation Agent:
- Plans documentation updates
- Tracks examples to update

Result: Coordinated implementation with quality gates
```

## 10. Migration Path for Existing Repos

For repositories already using caretaker:

### Automatic Upgrade
The upgrade agent will:
1. Detect old agent file format
2. Create skills directory
3. Add new agent files
4. Update copilot-instructions.md
5. Preserve existing config
6. Test all functionality
7. Create PR with changes

### Manual Opt-In Features
Some features require opt-in:
- MCP skill usage (requires endpoint config)
- Specialized agents (must enable in config)
- Advanced collaboration (beta feature)

### Breaking Changes
None - all changes are additive

## 11. Testing Strategy

### Skill Testing
- Unit tests for skill logic
- Integration tests with Claude Code
- Manual testing with real users
- Performance benchmarks
- Error handling validation

### Agent Testing
- State machine validation
- Protocol compliance tests
- End-to-end scenarios
- Failure recovery tests
- Multi-agent coordination tests

### Documentation Testing
- Examples are executable
- Links are valid
- Code samples work
- Instructions are complete
- Screenshots are current

## 12. Security Considerations

### Skills
- Skills cannot access secrets directly
- All file operations are logged
- Restricted to repo boundaries
- Rate limiting on expensive operations
- Audit trail for all actions

### Agents
- Agents have limited permissions
- Cannot modify .github/maintainer/ config
- Cannot force push
- Cannot delete branches without approval
- All actions are logged

### MCP
- Authentication required
- Authorization per skill
- Input validation
- Output sanitization
- Rate limiting

## 13. Community and Contribution

### Skill Contributions
Enable community-contributed skills:
1. Skill development guide
2. Skill submission process
3. Skill review criteria
4. Skill marketplace
5. Skill versioning

### Agent Contributions
Enable custom agents:
1. Agent template
2. Agent testing framework
3. Agent submission process
4. Agent registry
5. Agent sharing

### Documentation
All skills and agents should:
- Have clear documentation
- Include examples
- List dependencies
- Define success criteria
- Provide troubleshooting

## 14. Related Work

### Connections to Existing Plans
- **Azure MCP Plan**: MCP skill integration builds on this
- **Review Agent Plan**: Review agent is one of new specialized agents
- **Refactor Plan**: Skills help with refactoring tasks

### Dependencies
- MCP backend implementation
- Agent registry enhancements
- Configuration schema updates
- Documentation infrastructure

## 15. Open Questions

1. **Skill Naming**: Follow Claude Code conventions or create caretaker-specific ones?
2. **Agent Granularity**: How many specialized agents is too many?
3. **MCP Hosting**: Should skills run locally or always remote?
4. **Versioning**: How to version skills and agents separately from caretaker?
5. **Discovery**: How do users discover available skills?

## 16. Next Steps

1. Review and approve this plan
2. Create skills directory structure
3. Implement first 3 skills (setup, debug, config)
4. Enhance existing agent files
5. Add Claude Code guidelines
6. Update documentation
7. Test with real users
8. Iterate based on feedback

## Conclusion

This plan enhances caretaker with:
- **5 core skills** for human developers
- **5 new specialized agents** for advanced automation
- **Enhanced agent files** with better structure and examples
- **MCP skill integration** for powerful remote capabilities
- **Better developer experience** for both humans and AI

The approach is:
- **Incremental**: Build on existing architecture
- **Backward compatible**: No breaking changes
- **Well-documented**: Clear guides and examples
- **Tested**: Comprehensive test coverage
- **Extensible**: Easy to add more skills and agents

This positions caretaker as the premier autonomous repository maintenance system with best-in-class support for both human developers and AI coding agents.
