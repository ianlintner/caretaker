# Skill: caretaker-config

## Purpose

Help developers understand, configure, and customize caretaker behavior for their specific repository needs.

## Capabilities

- Explain all configuration options
- Generate configuration snippets for common scenarios
- Validate configuration files
- Tune settings for optimal performance
- Configure agent-specific behavior
- Set up LLM integration (Claude/Copilot)
- Adjust schedules and automation levels
- Troubleshoot configuration issues

## When to Use

- Initial caretaker setup (after basic installation)
- Customizing caretaker for specific repository needs
- Adjusting automation level (more/less aggressive)
- Enabling/disabling specific agents
- Configuring auto-merge policies
- Tuning for high/low activity repositories
- Setting up premium features (Claude integration)
- Debugging configuration problems

## Configuration Reference

### Full Configuration Structure

```yaml
version: v1  # Config schema version (required)

orchestrator:
  schedule: daily | weekly | hourly | manual
  summary_issue: true | false
  dry_run: true | false

pr_agent:
  enabled: true | false
  auto_merge:
    copilot_prs: true | false
    dependabot_prs: true | false
    human_prs: true | false
    merge_method: squash | merge | rebase
  copilot:
    max_retries: 2
    retry_window_hours: 24
    context_injection: true | false
  ci:
    flaky_retries: 1
    ignore_jobs: []
  review:
    auto_approve_copilot: true | false
    nitpick_threshold: low | medium | high

issue_agent:
  enabled: true | false
  auto_assign_bugs: true | false
  auto_assign_features: true | false
  labels:
    bug: ["bug", "Bug"]
    feature: ["enhancement", "feature"]
    question: ["question"]

upgrade_agent:
  enabled: true | false
  strategy: auto-minor | auto-patch | latest | pinned | manual
  channel: stable | preview
  auto_merge_non_breaking: true | false

dependency_agent:
  enabled: true | false
  auto_merge_minor: true | false
  auto_merge_patch: true | false

security_agent:
  enabled: true | false
  auto_fix_vulnerabilities: true | false

devops_agent:
  enabled: true | false
  auto_fix_ci: true | false

docs_agent:
  enabled: true | false
  auto_update_changelog: true | false

escalation:
  targets: []  # GitHub usernames, empty = repo owner
  stale_days: 7
  labels: ["maintainer:escalated"]

llm:
  claude_enabled: auto | true | false
  claude_features:
    - ci_log_analysis
    - architectural_review
    - issue_decomposition
    - upgrade_impact_analysis

memory_store:
  enabled: true | false
  db_path: .caretaker-memory.db
  max_entries_per_namespace: 1000
```

## Common Configuration Scenarios

### 1. Conservative / Safety-First

Use when: New to caretaker, high-value production repo

```yaml
version: v1

orchestrator:
  schedule: weekly
  summary_issue: true
  dry_run: false

pr_agent:
  enabled: true
  auto_merge:
    copilot_prs: false  # Manual review required
    dependabot_prs: false
    human_prs: false
    merge_method: squash

issue_agent:
  enabled: true
  auto_assign_bugs: false  # Manual triage
  auto_assign_features: false

upgrade_agent:
  enabled: true
  strategy: manual  # No automatic upgrades
  auto_merge_non_breaking: false

# Most agents disabled for safety
dependency_agent:
  enabled: false

security_agent:
  enabled: true
  auto_fix_vulnerabilities: false  # Alert only

devops_agent:
  enabled: false

escalation:
  stale_days: 3  # Quick escalation
```

### 2. Balanced / Recommended

Use when: Standard development repo, moderate activity

```yaml
version: v1

orchestrator:
  schedule: daily
  summary_issue: true
  dry_run: false

pr_agent:
  enabled: true
  auto_merge:
    copilot_prs: true  # Trust caretaker PRs
    dependabot_prs: true  # Auto-merge deps
    human_prs: false
    merge_method: squash
  copilot:
    max_retries: 2
    retry_window_hours: 24
    context_injection: true
  ci:
    flaky_retries: 1
    ignore_jobs: []

issue_agent:
  enabled: true
  auto_assign_bugs: true  # Auto-assign simple bugs
  auto_assign_features: false  # Manual feature review

upgrade_agent:
  enabled: true
  strategy: auto-minor  # Auto-upgrade minor versions
  channel: stable
  auto_merge_non_breaking: true

dependency_agent:
  enabled: true
  auto_merge_minor: false
  auto_merge_patch: true  # Auto patch updates only

security_agent:
  enabled: true
  auto_fix_vulnerabilities: true

devops_agent:
  enabled: true
  auto_fix_ci: true

docs_agent:
  enabled: true
  auto_update_changelog: true
```

### 3. Aggressive / Fully Automated

Use when: High-trust environment, low-risk repo, fast iteration

```yaml
version: v1

orchestrator:
  schedule: hourly  # Very frequent
  summary_issue: true
  dry_run: false

pr_agent:
  enabled: true
  auto_merge:
    copilot_prs: true
    dependabot_prs: true
    human_prs: true  # Even auto-merge human PRs (careful!)
    merge_method: squash
  copilot:
    max_retries: 3
    retry_window_hours: 12
  ci:
    flaky_retries: 2
  review:
    auto_approve_copilot: true  # Auto-approve

issue_agent:
  enabled: true
  auto_assign_bugs: true
  auto_assign_features: true  # Auto-assign features too

upgrade_agent:
  enabled: true
  strategy: latest  # Always latest
  auto_merge_non_breaking: true

dependency_agent:
  enabled: true
  auto_merge_minor: true  # Auto minor updates
  auto_merge_patch: true

security_agent:
  enabled: true
  auto_fix_vulnerabilities: true

devops_agent:
  enabled: true
  auto_fix_ci: true

llm:
  claude_enabled: true  # Use Claude for better decisions
  claude_features:
    - ci_log_analysis
    - architectural_review
    - issue_decomposition
```

### 4. Documentation-Only Repo

Use when: Docs/website repo, no code compilation

```yaml
version: v1

orchestrator:
  schedule: weekly
  summary_issue: false  # Less noise
  dry_run: false

pr_agent:
  enabled: true
  auto_merge:
    copilot_prs: true
    dependabot_prs: true
    merge_method: squash
  ci:
    ignore_jobs: []

issue_agent:
  enabled: true
  auto_assign_bugs: true

# Disable code-focused agents
upgrade_agent:
  enabled: true

dependency_agent:
  enabled: false  # No dependencies

security_agent:
  enabled: false  # No security concerns

devops_agent:
  enabled: false  # No complex CI

docs_agent:
  enabled: true
  auto_update_changelog: true
```

## Configuration Tuning Guide

### Tuning Schedule

| Repo Activity | Recommended Schedule | Reasoning |
|---------------|---------------------|-----------|
| <2 commits/day | weekly | Low activity, weekly is sufficient |
| 2-10 commits/day | daily | Moderate activity, daily keeps up |
| >10 commits/day | hourly | High activity, need frequent checks |
| On-demand | manual | Use workflow_dispatch trigger only |

### Tuning Auto-Merge

**When to enable copilot_prs auto-merge:**
- ✅ You trust caretaker's judgment
- ✅ You have comprehensive tests
- ✅ PRs are small and focused
- ✅ You review commit history regularly

**When to disable:**
- ❌ Critical production system
- ❌ Compliance/audit requirements
- ❌ Learning caretaker behavior
- ❌ Incomplete test coverage

### Tuning Agent Enablement

Enable agents based on repo needs:

| Agent | Enable When | Disable When |
|-------|-------------|--------------|
| pr_agent | Always | Never (core agent) |
| issue_agent | Have external contributors | Private/internal repo only |
| upgrade_agent | Want auto-updates | Pinned versions required |
| dependency_agent | Use package managers | No dependencies |
| security_agent | Have security concerns | Low-risk projects |
| devops_agent | Complex CI | Simple/no CI |
| docs_agent | Have changelog | No docs/changelog |

## Validation and Testing

### Validate Configuration

```bash
# Validate YAML syntax
yamllint .github/maintainer/config.yml

# Check against schema (if caretaker provides)
caretaker validate --config .github/maintainer/config.yml

# Dry-run to test
caretaker run --config .github/maintainer/config.yml --mode dry-run
```

### Test Changes

```bash
# 1. Test locally first
caretaker run --config .github/maintainer/config.yml --mode dry-run

# 2. Commit and push
git add .github/maintainer/config.yml
git commit -m "chore: update caretaker config"
git push

# 3. Trigger manual run
gh workflow run maintainer.yml

# 4. Monitor results
gh run watch
```

## Troubleshooting

### Configuration Not Taking Effect

**Symptom**: Changes to config don't seem to apply

**Solutions**:
1. Verify config file is committed and pushed
2. Check file path is exactly `.github/maintainer/config.yml`
3. Validate YAML syntax
4. Check workflow is reading correct file
5. Wait for next scheduled run or trigger manually

### Validation Errors

**Symptom**: "Invalid configuration" errors

**Solutions**:
1. Check YAML syntax (indentation, quotes)
2. Verify all required fields present
3. Check field names for typos
4. Ensure values are valid (true/false, valid enums)
5. Compare against working example

### Unexpected Behavior

**Symptom**: Caretaker doing more/less than expected

**Solutions**:
1. Review auto_merge settings
2. Check agent enabled status
3. Verify schedule is appropriate
4. Check escalation settings
5. Review logs for clues

## Related Skills

- **[caretaker-setup](./caretaker-setup.md)** - Initial setup
- **[caretaker-debug](./caretaker-debug.md)** - Debugging issues
- **[caretaker-agent-dev](./caretaker-agent-dev.md)** - Custom agents

## Additional Resources

- [Configuration Documentation](../../docs/configuration.md)
- [Config Schema](../../schema/config.v1.schema.json)
- [Default Config Template](../../dist/templates/config-default.yml)

## Notes

- Configuration is validated at runtime
- Invalid configs cause workflow to fail fast
- Changes take effect on next caretaker run
- Some settings require repository permissions
- Test config changes in dry-run mode first

## Version History

- v1.0 - Initial skill (2026-04-17)
