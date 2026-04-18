# PR Ownership and Caretaker Merge Authority

## Overview

PR Ownership is a first-class capability in Caretaker that establishes Caretaker as the authoritative system for PR readiness evaluation and merge management. Ownership is marked by the `caretaker:owned` label and the `caretaker/pr-readiness` check run.

## Key Concepts

### Ownership State

PRs can be in one of four ownership states:

| State | Description |
|-------|-------------|
| `unowned` | Caretaker is not managing this PR |
| `owned` | Caretaker has claimed responsibility for this PR |
| `released` | Caretaker has released ownership (merged, closed, or manual release) |
| `escalated` | Caretaker has escalated to maintainers and released ownership |

### Ownership Label

The `caretaker:owned` label marks PRs that Caretaker has claimed. This label:

- Is automatically added when Caretaker claims a PR
- Is visible in the GitHub PR UI
- Can be manually added by maintainers to opt-in human PRs

### Hold Label

The `caretaker:hold` label prevents Caretaker from merging a PR even if it meets all other criteria:

- Maintained by humans who want to control merge timing
- Does not affect ownership claim
- Affects readiness evaluation by adding `manual_hold` blocker and withholding the 10% mergeability component

## Readiness Scoring

Caretaker evaluates PRs on a 0.0-1.0 readiness score:

| Component | Points | Requirements |
|-----------|--------|-------------|
| Mergeable & non-draft | 10% | PR is not a draft, has no merge conflicts, no `maintainer:breaking` or `caretaker:hold` label |
| Automated feedback addressed | 20% | No pending automated review comments OR fix already requested |
| Required reviews satisfied | 30% | Reviews approved, no `CHANGES_REQUESTED` |
| CI passing | 40% | All checks passing, no pending checks |

### Readiness Check Conclusions

- `success`: Score 1.0, no blockers — PR is ready for merge
- `failure`: Explicit blockers remain — PR cannot be merged
- `in_progress`: Checks/reviews still pending — PR not yet ready

### Standard Blocker Codes

| Code | Description |
|------|-------------|
| `ci_pending` | CI checks are still running |
| `ci_failing` | One or more CI checks have failed |
| `required_review_missing` | No approving reviews yet |
| `changes_requested` | Reviewer has requested changes |
| `automated_feedback_unaddressed` | Automated review comments not yet addressed |
| `draft_pr` | PR is still in draft mode |
| `merge_conflict` | PR has merge conflicts |
| `breaking_change` | PR has `maintainer:breaking` label |
| `manual_hold` | PR has `caretaker:hold` label |

## Phased Rollout

### Phase 1: Advisory (Current)

**Goal**: Ship ownership state, label, comments, and readiness score without changing merge behavior.

Features shipped:
- ✅ Ownership state tracking in `TrackedPR`
- ✅ Readiness score calculation
- ✅ `caretaker/pr-readiness` check (non-required)
- ✅ Ownership claim/release comments
- ✅ Automatic claim for Copilot and Dependabot PRs
- ✅ Manual opt-in for human PRs via `caretaker:owned` label

Current behavior:
- Caretaker evaluates PRs and publishes readiness
- Existing merge gating and auto-merge behavior unchanged
- No breaking changes to existing workflows

### Phase 2: Required Check

**Goal**: Make `caretaker/pr-readiness` a required ruleset check.

What changes:
- Configure GitHub branch protection rules to require `caretaker/pr-readiness`
- The check must pass before PRs can be merged
- Caretaker check conclusion (`success`, `failure`, `in_progress`) gates merge

Branch protection setup:
1. Navigate to repository Settings → Branches → Branch protection rules
2. Create/edit rule for target branch (e.g., `main`)
3. Under "Status checks", add `caretaker/pr-readiness`
4. Mark as "Required" before merging
5. Ensure Caretaker GitHub App is the check source

### Phase 3: Merge Authority

**Goal**: Caretaker merges owned PRs directly when readiness is `success`.

New configuration option `merge_authority.mode`:
- `advisory`: Current behavior (Phase 1/2)
- `gate_only`: Gate via required check, don't merge directly
- `gate_and_merge`: Gate AND merge when ready (default for Copilot/Dependabot)

Changes:
- `pr_agent.auto_merge` behavior replaced by `pr_agent.merge_authority`
- Caretaker calls merge API after readiness check is `success`
- 405/409/422 errors treated as non-terminal (keep ownership, retry next cycle)
- Escalation triggers ownership release

## Configuration

### Ownership Configuration

```yaml
pr_agent:
  ownership:
    enabled: true
    auto_claim:
      copilot_prs: true      # Auto-claim Copilot PRs
      dependabot_prs: true    # Auto-claim Dependabot PRs
      human_prs: false        # Human PRs require manual opt-in
    label: caretaker:owned   # Ownership label name
    hold_label: caretaker:hold  # Manual hold label
```

### Readiness Configuration

```yaml
pr_agent:
  readiness:
    enabled: true
    check_name: caretaker/pr-readiness
    required_reviews: 1
    require_all_checks_passed: true
    require_review_resolution: true
```

### Merge Authority Configuration (Phase 3)

```yaml
pr_agent:
  merge_authority:
    mode: advisory  # advisory | gate_only | gate_and_merge
```

## GitHub App Integration

Caretaker uses the GitHub App identity for:
- Publishing check runs (`caretaker/pr-readiness`)
- Adding labels (`caretaker:owned`, `maintainer:escalated`)
- Posting comments

The GitHub App should be:
- Installed on repositories using Caretaker
- Given write permissions for:
  - Checks (read/write)
  - Pull requests (read/write)
  - Issues (read/write)
  - Contents (read) - for code analysis if needed

## Run Summary Metrics

Ownership metrics are exposed in run summaries:

| Metric | Description |
|--------|-------------|
| `owned_prs` | Number of PRs currently owned by Caretaker |
| `readiness_pass_rate` | Percentage of owned PRs with score >= 1.0 |
| `avg_readiness_score` | Average readiness score across owned PRs |
| `authority_merges` | Number of merges performed by Caretaker authority |

## Migration from Legacy Auto-Merge

To migrate from the current `auto_merge` behavior to Caretaker merge authority:

1. **Phase 1**: Deploy ownership tracking without changing auto-merge
   ```yaml
   pr_agent:
     auto_merge:
       # Existing settings
     ownership:
       enabled: true
     readiness:
       enabled: true
   ```

2. **Phase 2**: Enable required check in branch protection
   - Configure ruleset to require `caretaker/pr-readiness`
   - Keep `auto_merge` settings active as fallback

3. **Phase 3**: Switch to merge authority
   ```yaml
   pr_agent:
     auto_merge:
       copilot_prs: false  # Disable legacy auto-merge
       dependabot_prs: false
       human_prs: false
     merge_authority:
       mode: gate_and_merge
   ```

## Backward Compatibility

- Repositories without ownership enabled retain existing behavior
- The `auto_merge` configuration continues to work until Phase 3 migration
- No breaking changes in Phase 1 or Phase 2

## Known Limitations

- Check runs require a PR head commit SHA; if GitHub omits `head.sha`, readiness check publication is skipped for that cycle
- Assignee APIs are user-oriented; Caretaker uses labels/checks/comments for identity
