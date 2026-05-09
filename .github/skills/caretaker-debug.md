# Skill: caretaker-debug

## Purpose

Help developers diagnose and fix issues with caretaker in their repositories.

## Capabilities

- Analyze caretaker run logs from GitHub Actions
- Diagnose configuration problems
- Check GitHub Actions setup and permissions
- Identify agent execution failures
- Suggest fixes for common issues
- Validate caretaker installation
- Debug webhook and event handling
- Troubleshoot CI integration problems

## When to Use

- Caretaker workflow failing in GitHub Actions
- Agents not executing or behaving incorrectly
- Configuration errors or validation failures
- PRs not being auto-merged when expected
- Issues not being triaged or assigned
- Upgrade process failing
- Permissions or authentication errors
- Any unexpected caretaker behavior

## Prerequisites

- Access to GitHub Actions logs
- Read access to repository settings
- Basic understanding of YAML and GitHub Actions

## Usage Examples

### Example 1: Workflow Failing with "Config not found"

**Symptom**: GitHub Actions run fails immediately with error:

```
Error: Configuration file not found: .github/maintainer/config.yml
```

**Diagnosis**:

1. Check file exists: `ls .github/maintainer/config.yml`
2. Check file is committed: `git ls-files .github/maintainer/config.yml`
3. Check branch: Is workflow running on correct branch?

**Solution**:

```bash
# File doesn't exist - create it
mkdir -p .github/maintainer
cp dist/templates/config-default.yml .github/maintainer/config.yml
git add .github/maintainer/config.yml
git commit -m "chore: add caretaker config"
```

**Prevention**: Always commit config file before enabling workflow.

### Example 3: Agent Not Executing

**Symptom**: Caretaker runs but PR agent doesn't process PRs.

**Diagnosis**:

1. Check agent is enabled in config:

```yaml
pr_agent:
  enabled: true # ← Must be true
```

2. Check logs for agent errors:

```
[PR Agent] Skipped: disabled in config
[PR Agent] Error: ...
```

3. Check event payload:

```
# Verify PR event is being received
Event type: pull_request
Event action: opened
```

**Solution**:

```yaml
# Enable agent in config
pr_agent:
  enabled: true
```

**Prevention**: Use default config template.

### Example 4: Auto-Merge Not Working

**Symptom**: PRs that should auto-merge are not merging.

**Diagnosis**:

1. Check auto-merge configuration:

```yaml
pr_agent:
  auto_merge:
    copilot_prs: true # ← For Copilot PRs
    dependabot_prs: true # ← For Dependabot PRs
    human_prs: false # ← Usually false for safety
```

2. Check PR meets criteria:
   - ✓ CI is passing (all required checks)
   - ✓ Approved by reviewer (if required)
   - ✓ No merge conflicts
   - ✓ Not in draft mode
   - ✓ PR author matches auto-merge rules

3. Check branch protection:
   - Required status checks are passing
   - Required reviewers have approved
   - No blocking restrictions

**Solution**:

```yaml
# Adjust auto-merge settings
pr_agent:
  auto_merge:
    copilot_prs: true
    dependabot_prs: true
    merge_method: squash

  ci:
    flaky_retries: 1 # Retry flaky CI once
    ignore_jobs: [] # Don't ignore any jobs

  review:
    auto_approve_copilot: false # Don't auto-approve (unless configured)
```

**Prevention**: Verify branch protection rules align with auto-merge config.

### Example 5: High Error Rate in Logs

**Symptom**: Lots of errors in GitHub Actions logs.

**Diagnosis**:

1. Identify error patterns:

```bash
# Download logs and analyze
gh run view <run-id> --log | grep -i error

# Common patterns:
# - "Rate limit exceeded" → Too many API calls
# - "Timeout" → Operations taking too long
# - "Not found" → Missing resources
# - "Validation failed" → Invalid data
```

2. Check API rate limits:

```bash
# Check current rate limit status
gh api rate_limit
```

3. Check for transient vs persistent errors

**Solution**:

```yaml
# For rate limiting issues, reduce frequency
orchestrator:
  schedule: weekly # Instead of daily

# For timeout issues, increase timeouts
llm:
  claude_timeout_seconds: 300 # Increase timeout
```

**Prevention**: Monitor rate limits and adjust schedule accordingly.

## Implementation Guide

### For Claude Code

When a user reports a caretaker issue:

1. **Gather information**:

   ```
   To help debug, I need:
   1. Error message or symptom
   2. Link to failed GitHub Actions run
   3. Relevant configuration files
   4. Recent changes to caretaker setup
   ```

2. **Analyze logs**:

   ```python
   # Fetch and parse GitHub Actions logs
   logs = fetch_actions_logs(run_id)

   # Look for key indicators
   errors = extract_errors(logs)
   warnings = extract_warnings(logs)
   agent_execution = extract_agent_logs(logs)
   ```

3. **Check configuration**:

   ```python
   # Validate config file
   config = load_config('.github/maintainer/config.yml')
   validation_errors = validate_config(config)

   # Check version compatibility
   caretaker_version = read_version('.github/maintainer/.version')
   is_compatible = check_compatibility(caretaker_version, config)
   ```

4. **Diagnose issue**:
   - Match error patterns to known issues
   - Check for common misconfigurations
   - Verify prerequisites are met
   - Identify root cause

5. **Propose solution**:
   - Provide specific fix
   - Explain why it will work
   - Show exact changes needed
   - Offer validation steps

6. **Verify fix**:
   - Apply changes
   - Re-run workflow
   - Confirm issue resolved
   - Document for future reference

### For Copilot

When debugging caretaker issues:

1. **Read error logs** thoroughly
2. **Identify** specific error message or code
3. **Search** for similar issues in caretaker docs
4. **Apply** fix from troubleshooting guide
5. **Test** to verify fix works
6. **Document** fix in PR or issue comment

## Common Patterns

### Log Analysis Pattern

```python
def diagnose_from_logs(logs: str) -> Diagnosis:
    """Analyze GitHub Actions logs to diagnose issues."""

    diagnosis = Diagnosis()

    # Check for config issues
    if "config not found" in logs.lower():
        diagnosis.add_issue(
            issue="Missing configuration file",
            fix="Create .github/maintainer/config.yml"
        )

    # Check for permission issues
    if "permission denied" in logs.lower():
        diagnosis.add_issue(
            issue="Insufficient permissions",
            fix="Add required permissions to workflow file"
        )

    # Check for version issues
    if "version" in logs.lower() and "not found" in logs.lower():
        diagnosis.add_issue(
            issue="Invalid caretaker version",
            fix="Update .github/maintainer/.version to valid version"
        )

    # Check for agent failures
    agent_errors = extract_agent_errors(logs)
    for error in agent_errors:
        diagnosis.add_agent_issue(error)

    return diagnosis
```

### Configuration Validation Pattern

```python
def validate_configuration(config_path: str) -> ValidationResult:
    """Validate caretaker configuration."""

    result = ValidationResult()

    # Load config
    try:
        config = yaml.safe_load(open(config_path))
    except yaml.YAMLError as e:
        result.add_error(f"Invalid YAML: {e}")
        return result

    # Check version
    if config.get('version') != 'v1':
        result.add_error("Invalid config version")

    # Check required sections
    required = ['orchestrator', 'pr_agent', 'issue_agent']
    for section in required:
        if section not in config:
            result.add_error(f"Missing required section: {section}")

    # Validate agent configs
    for agent, agent_config in config.items():
        if agent.endswith('_agent'):
            validate_agent_config(agent, agent_config, result)

    return result
```

## Troubleshooting

### Issue: Very slow execution

**Symptoms**:

- Workflow takes >10 minutes
- Timeout errors
- Rate limiting issues

**Diagnosis**:

1. Check for unnecessary API calls
2. Check if analyzing too many PRs/issues
3. Check network latency
4. Check if Claude integration is slow

**Solutions**:

```yaml
# Reduce scope
pr_agent:
  ci:
    ignore_jobs: ["optional-job"] # Ignore non-critical jobs

# Increase timeouts
llm:
  claude_timeout_seconds: 300

# Reduce frequency
orchestrator:
  schedule: weekly # Instead of daily
```

### Issue: Agent results not persisting

**Symptoms**:

- Agents seem to work but changes aren't visible
- State doesn't carry over between runs
- Memory doesn't persist

**Diagnosis**:

1. Check if changes are being committed
2. Check if state tracking is configured
3. Check memory store settings

**Solutions**:

```yaml
# Enable state persistence
memory_store:
  enabled: true
  db_path: .caretaker-memory.db

# Ensure proper permissions for writing
permissions:
  contents: write
```

### Issue: Claude integration not working

**Symptoms**:

- Claude features disabled despite key being set
- Errors about Claude API
- Falling back to simpler logic

**Diagnosis**:

1. Check if ANTHROPIC_API_KEY secret is set
2. Check if key is valid
3. Check if Claude features are enabled in config
4. Check API connectivity

**Solutions**:

```bash
# Set secret in GitHub
gh secret set ANTHROPIC_API_KEY --body "sk-ant-..."

# Enable in config
llm:
  claude_enabled: true
  claude_features:
    - ci_log_analysis
    - issue_decomposition
```

### Issue: Copilot not responding to tasks

**Symptoms**:

- Task comments posted but no response
- Copilot doesn't create PRs as expected
- Long delays with no activity

**Diagnosis**:

1. Check if @copilot is mentioned correctly
2. Check if agent files are present and readable
3. Check if task format is correct
4. Check Copilot service status

**Solutions**:

```markdown
<!-- Ensure proper task format -->

@copilot

<!-- caretaker:task -->

TASK: Fix CI failure
TYPE: TEST_FAILURE
... rest of task

<!-- /caretaker:task -->
```

## Debugging Tools

### 1. Validate Configuration

```bash
# If caretaker provides validation command
caretaker validate --config .github/maintainer/config.yml
```

### 2. Check Logs

```bash
# Run caretaker locally in dry-run mode to see output
catetaker run \
  --config .github/maintainer/config.yml \
  --mode dry-run
```

### 3. Test Locally

```bash
# Run caretaker locally in dry-run mode
caretaker run \
  --config .github/maintainer/config.yml \
  --mode dry-run
```

### 4. Verify Installation

```bash
# Check installed version
pip show caretaker

# Check version file
cat .github/maintainer/.version

# Compare versions
echo "Installed: $(pip show caretaker | grep Version)"
echo "Configured: $(cat .github/maintainer/.version)"
```

## Related Skills

- **[caretaker-setup](./caretaker-setup.md)** - Initial setup guide
- **[caretaker-config](./caretaker-config.md)** - Configuration reference
- **[caretaker-agent-dev](./caretaker-agent-dev.md)** - Agent development guide

## Additional Resources

- [GitHub Actions Documentation](https://docs.github.com/en/actions)
- [Caretaker Troubleshooting Guide](../../docs/troubleshooting.md)
- [GitHub Status](https://www.githubstatus.com/)
- [Caretaker Issues](https://github.com/ianlintner/caretaker/issues)

## Notes

- Always check GitHub Actions logs first
- Most issues are configuration or permission related
- Dry-run mode is safe for testing
- Keep caretaker updated to latest version
- Report persistent issues on GitHub

## Version History

- v1.0 - Initial skill (2026-04-17)
