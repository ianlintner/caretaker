# Skill: caretaker-setup

## Purpose

Guide developers through setting up the caretaker automated repository maintenance system in any repository.

## Capabilities

- Analyze repository structure (languages, CI systems, branch protection)
- Generate appropriate `config.yml` with sensible defaults
- Create GitHub Actions workflow file
- Set up agent persona files for Copilot
- Configure Copilot instructions
- Validate the complete setup
- Create a single PR with all changes

## When to Use

- Setting up caretaker in a repository for the first time
- Migrating from manual maintenance to automated maintenance
- Adding caretaker to an existing project
- Reconfiguring caretaker after major repo changes

## Prerequisites

- Repository admin access or write permissions
- GitHub Actions enabled
- Basic familiarity with GitHub workflows (helpful but not required)

## Usage Examples

### Example 1: Setup in a Python Project

**Scenario**: Setting up caretaker in a Python project with pytest, ruff, and mypy.

**Steps**:

1. **Analyze the repository**:
   - Detect language: Python (via pyproject.toml, setup.py, or .py files)
   - Detect CI: GitHub Actions (via .github/workflows/)
   - Detect testing: pytest (via pyproject.toml or pytest.ini)
   - Detect linting: ruff (via pyproject.toml or ruff.toml)
   - Detect typing: mypy (via pyproject.toml or mypy.ini)
   - Check default branch: main or master

2. **Generate configuration** (`.github/maintainer/config.yml`):
```yaml
version: v1

orchestrator:
  schedule: daily  # Python projects often have daily deps
  summary_issue: true
  dry_run: false

pr_agent:
  enabled: true
  auto_merge:
    copilot_prs: true
    dependabot_prs: true
    human_prs: false
    merge_method: squash
  copilot:
    max_retries: 2
    retry_window_hours: 24
    context_injection: true
  ci:
    flaky_retries: 1
    ignore_jobs: []
  review:
    auto_approve_copilot: false
    nitpick_threshold: low

issue_agent:
  enabled: true
  auto_assign_bugs: true
  auto_assign_features: false
  labels:
    bug: ["bug", "Bug"]
    feature: ["enhancement", "feature"]
    question: ["question"]

upgrade_agent:
  enabled: true
  strategy: auto-minor
  channel: stable
  auto_merge_non_breaking: true

dependency_agent:
  enabled: true
  auto_merge_minor: true
  auto_merge_patch: true

security_agent:
  enabled: true
  auto_fix_vulnerabilities: true

devops_agent:
  enabled: true
  auto_fix_ci: true

docs_agent:
  enabled: true
  auto_update_changelog: true

escalation:
  targets: []  # Defaults to repo owner
  stale_days: 7
  labels: ["maintainer:escalated"]

llm:
  claude_enabled: auto
  claude_features:
    - ci_log_analysis
    - issue_decomposition
```

3. **Create workflow file** (`.github/workflows/maintainer.yml`):
```yaml
name: Caretaker

on:
  schedule:
    - cron: '0 8 * * *'  # Daily at 8 AM UTC
  pull_request:
    types: [opened, synchronize, reopened]
  pull_request_review:
    types: [submitted]
  check_suite:
    types: [completed]
  issues:
    types: [opened, labeled]
  issue_comment:
    types: [created]
  workflow_dispatch:
    inputs:
      mode:
        description: 'Run mode'
        required: false
        default: 'full'
        type: choice
        options: [full, pr-only, issue-only, upgrade-only, dry-run]

permissions:
  contents: write
  issues: write
  pull-requests: write
  checks: read

jobs:
  maintain:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'
          cache: 'pip'

      - name: Install caretaker
        run: |
          VERSION=$(cat .github/maintainer/.version)
          pip install "caretaker==${VERSION}"

      - name: Run caretaker
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
        run: |
          caretaker run \
            --config .github/maintainer/config.yml \
            --mode "${{ github.event.inputs.mode || 'full' }}" \
            --event-type "${{ github.event_name }}" \
            --event-payload '${{ toJSON(github.event) }}'
```

4. **Pin version** (`.github/maintainer/.version`):
```
0.5.2
```

5. **Update Copilot instructions** (`.github/copilot-instructions.md`):
Append caretaker-specific instructions (if file exists) or create it.

6. **Create agent persona files**:
   - `.github/agents/maintainer-pr.md`
   - `.github/agents/maintainer-issue.md`
   - `.github/agents/maintainer-upgrade.md`
   - `.github/agents/karpathy-guidelines.md`

7. **Open PR** with all changes:
   - Title: "chore: set up caretaker automated maintenance"
   - Description: Summary of what was configured
   - Label: "maintainer:setup"

**Result**: Caretaker is fully configured and ready to run.

### Example 2: Setup in a TypeScript/Node.js Project

**Scenario**: Setting up caretaker in a Node.js project with npm, ESLint, and Jest.

**Steps**:

1. **Analyze**:
   - Language: TypeScript/JavaScript
   - Package manager: npm (via package-lock.json)
   - Testing: Jest (via package.json)
   - Linting: ESLint (via .eslintrc)
   - CI: GitHub Actions

2. **Generate config** with Node-specific settings:
```yaml
# Similar to Python but with:
orchestrator:
  schedule: weekly  # JS deps change less frequently

dependency_agent:
  enabled: true
  auto_merge_minor: false  # More conservative for JS
  auto_merge_patch: true
  package_manager: npm
```

3. **Workflow adjustments**:
```yaml
# Uses Node.js setup instead of Python:
- uses: actions/setup-node@v4
  with:
    node-version: '20'
    cache: 'npm'
```

4. **Complete setup** following same pattern as Python example.

### Example 3: Minimal Setup for Small Project

**Scenario**: Setting up caretaker in a small documentation-only repo.

**Configuration**:
```yaml
version: v1

orchestrator:
  schedule: weekly
  summary_issue: false
  dry_run: false

pr_agent:
  enabled: true
  auto_merge:
    copilot_prs: true
    dependabot_prs: true
    merge_method: squash

issue_agent:
  enabled: true
  auto_assign_bugs: true

upgrade_agent:
  enabled: true

# Disable agents not needed for docs:
dependency_agent:
  enabled: false

security_agent:
  enabled: false

devops_agent:
  enabled: false
```

## Implementation Guide

### For Claude Code

When a user says "set up caretaker" or similar:

1. **Confirm intent**:
   ```
   I'll help you set up caretaker for automated repository maintenance.
   This will:
   - Analyze your repo structure
   - Generate configuration files
   - Set up GitHub Actions workflow
   - Configure AI agent personas
   - Open a PR with all changes

   Proceed? (yes/no)
   ```

2. **Analyze repository**:
   ```python
   # Detect languages
   languages = detect_languages()  # Look for language-specific files

   # Detect CI
   has_ci = check_path(".github/workflows/")

   # Detect testing
   testing_framework = detect_testing()  # pytest, jest, etc.

   # Detect linting
   linters = detect_linters()  # ruff, eslint, etc.

   # Get default branch
   default_branch = get_default_branch()
   ```

3. **Generate configuration**:
   - Use language-appropriate defaults
   - Enable relevant agents
   - Set sensible schedules
   - Configure auto-merge policies

4. **Create required files**:
   - `.github/maintainer/config.yml`
   - `.github/maintainer/.version`
   - `.github/workflows/maintainer.yml`
   - `.github/agents/*.md` (if not exist)

5. **Update existing files**:
   - Append to `.github/copilot-instructions.md` (don't overwrite)
   - Add .gitignore entries if needed

6. **Validate setup**:
   - Check all files are valid YAML/markdown
   - Verify version number is valid
   - Confirm workflow syntax is correct

7. **Open PR**:
   - Create branch: `setup-caretaker`
   - Commit all changes
   - Open PR with descriptive title and body
   - Add setup checklist to PR description

8. **Provide next steps**:
   ```
   Setup complete! Next steps:
   1. Review the PR I opened
   2. (Optional) Add ANTHROPIC_API_KEY secret for premium features
   3. Merge the PR to activate caretaker
   4. Caretaker will run on the configured schedule
   ```

### For Copilot

When assigned a setup issue by caretaker:

1. **Read setup guide**: Fetch from caretaker repo if needed
2. **Analyze repo**: Use available tools to inspect structure
3. **Generate files**: Create config, workflow, agents
4. **Follow conventions**: Use patterns from this skill
5. **Test locally**: Validate YAML and file syntax
6. **Open PR**: Single PR with all changes
7. **Report**: Comment on issue with PR link

## Common Patterns

### Configuration Generation Template

```python
def generate_config(repo_info: RepoInfo) -> dict:
    """Generate caretaker config based on repo characteristics."""

    config = {
        "version": "v1",
        "orchestrator": {
            "schedule": get_schedule(repo_info.activity_level),
            "summary_issue": True,
            "dry_run": False,
        },
        "pr_agent": {
            "enabled": True,
            "auto_merge": {
                "copilot_prs": True,
                "dependabot_prs": repo_info.has_dependabot,
                "human_prs": False,
                "merge_method": get_merge_method(repo_info),
            },
        },
        # ... rest of config
    }

    # Add language-specific settings
    if repo_info.language == "python":
        config = add_python_settings(config)
    elif repo_info.language in ["javascript", "typescript"]:
        config = add_node_settings(config)

    # Enable appropriate agents
    config = enable_agents_for_repo(config, repo_info)

    return config
```

### Schedule Selection

```python
def get_schedule(activity_level: str) -> str:
    """Choose appropriate schedule based on repo activity."""
    if activity_level == "high":  # >10 commits/day
        return "daily"
    elif activity_level == "medium":  # 2-10 commits/day
        return "daily"
    else:  # <2 commits/day
        return "weekly"
```

### Merge Method Selection

```python
def get_merge_method(repo_info: RepoInfo) -> str:
    """Choose appropriate merge method."""
    # Check if repo uses conventional commits
    if repo_info.uses_conventional_commits:
        return "squash"  # Squash preserves clean history

    # Check if repo values individual commits
    if repo_info.detailed_commit_history:
        return "merge"  # Merge preserves all commits

    # Default to squash for clean history
    return "squash"
```

## Troubleshooting

### Issue: "Workflow syntax error"

**Symptom**: GitHub Actions shows syntax error in workflow file

**Cause**: Invalid YAML syntax or incorrect workflow structure

**Solution**:
1. Validate YAML syntax: `yamllint .github/workflows/maintainer.yml`
2. Check indentation (use spaces, not tabs)
3. Verify all required fields are present
4. Test workflow syntax with GitHub's workflow validator

### Issue: "Permission denied"

**Symptom**: Workflow fails with permission errors

**Cause**: Insufficient permissions in workflow file

**Solution**:
1. Check `permissions:` block includes:
   ```yaml
   permissions:
     contents: write
     issues: write
     pull-requests: write
     checks: read
   ```
2. Verify repo settings allow Actions to create PRs
3. Check branch protection rules don't block caretaker

### Issue: "Version not found"

**Symptom**: `pip install` fails to find caretaker version

**Cause**: Invalid version in `.github/maintainer/.version`

**Solution**:
1. Check latest version: https://pypi.org/project/caretaker/
2. Update `.version` file with valid version
3. Use format: `X.Y.Z` (e.g., `0.5.2`)

### Issue: "Config validation failed"

**Symptom**: Caretaker reports invalid configuration

**Cause**: Config doesn't match schema

**Solution**:
1. Validate against schema: `caretaker validate --config .github/maintainer/config.yml`
2. Check for typos in field names
3. Verify all required fields are present
4. Review [configuration docs](../../docs/configuration.md)

### Issue: "Agents not being created"

**Symptom**: Agent persona files are missing

**Cause**: Files weren't copied or created

**Solution**:
1. Fetch templates from caretaker repo
2. Copy to `.github/agents/`
3. Verify files exist and are readable
4. Check file permissions

## Related Skills

- **[caretaker-config](./caretaker-config.md)** - Configure and tune caretaker
- **[caretaker-debug](./caretaker-debug.md)** - Debug setup issues
- **[caretaker-upgrade](./caretaker-upgrade.md)** - Upgrade caretaker version

## Additional Resources

- [Caretaker Documentation](../../docs/)
- [Configuration Reference](../../docs/configuration.md)
- [Architecture Plan](../../plan.md)
- [Setup Agent](../../dist/SETUP_AGENT.md)

## Notes

- Setup is idempotent - running multiple times is safe
- All changes go in one PR for easy review
- Configuration can be customized after initial setup
- Agent personas can be modified to fit team needs
- Schedule can be adjusted based on repo activity

## Version History

- v1.0 - Initial skill (2026-04-17)
