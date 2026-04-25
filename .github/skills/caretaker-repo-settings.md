# Skill: caretaker-repo-settings

## Purpose

Audit and apply the ideal GitHub repository settings for repos managed by caretaker.
Covers merge settings, Actions permissions, and branch protection — the three surface
areas most likely to cause caretaker failures or noise if misconfigured.

## Capabilities

- Audit current repo settings against caretaker's ideal target state
- Apply missing/incorrect settings via `gh api` REST calls
- Generate a gap report (exit 0 = clean, exit 1 = gaps found)
- Work across any `OWNER/REPO` (parameterised)
- Document which settings require admin PAT vs standard GITHUB_TOKEN

## When to Use

- Setting up caretaker in a new repo (after `caretaker-setup`)
- Diagnosing mysterious caretaker failures (update-releases-json failing, auto-merge not working)
- Periodic drift-check across the fleet
- After a repo is transferred or forked
- When opening a new consumer repo to caretaker management

## Prerequisites

- `gh` CLI authenticated (`gh auth login`)
- For branch protection: a PAT with `repo` scope + admin access stored as `ADMIN_PAT` env var (GITHUB_TOKEN lacks admin perms)
- For Actions workflow permissions: admin access to the repo

---

## Target state reference

### A. Repository top-level settings

These control merge strategy, branch cleanup, and auto-merge availability.

| Setting | Target | Why |
|---------|--------|-----|
| `delete_branch_on_merge` | `true` | Prevents stale branch accumulation; stale_agent relies on branch cleanup |
| `allow_squash_merge` | `true` | pr_agent uses squash by default for clean history |
| `allow_merge_commit` | `false` | Prevents accidental non-squash merges |
| `allow_rebase_merge` | `false` | Consistency — one merge method across the board |
| `allow_auto_merge` | `true` | Enables GitHub's auto-merge queue; pr_agent can queue a merge before CI completes |

### B. Actions workflow permissions

Controls whether GitHub Actions workflows can create PRs and issues.

| Setting | Target | Why |
|---------|--------|-----|
| `default_workflow_permissions` | `write` | Caretaker workflows need `contents: write`, `pull-requests: write` |
| `can_approve_pull_request_reviews` | `true` | **Critical**: without this, `update-releases-json.yml` and similar release workflows cannot call `gh pr create` or approve PRs |

### C. Branch protection on `main`

Prevents force-pushes and gates merges on CI passing.

| Field | Target | Why |
|-------|--------|-----|
| `required_status_checks.strict` | `false` | Strict requires branch to be up-to-date; caretaker squash-merges create churn with strict mode |
| `required_status_checks.contexts` | `["lint", "test"]` (minimum) | CI must pass before merge |
| `required_pull_request_reviews.required_approving_review_count` | `1` | At least one approval; caretaker auto-approve satisfies this |
| `required_pull_request_reviews.dismiss_stale_reviews` | `false` | caretaker re-approves on new SHA via `last_approved_sha` (v0.19.3+); stale dismissal causes approve-dismiss loops |
| `required_pull_request_reviews.require_code_owner_reviews` | `false` | Not applicable to automated PRs |
| `enforce_admins` | `false` | Caretaker acts at admin level; enforcing for admins blocks its own merges |
| `restrictions` | `null` | No push restrictions |
| `required_linear_history` | `true` | Squash-merge produces linear history; this enforces consistency |
| `allow_force_pushes` | `false` | Protect main |
| `allow_deletions` | `false` | Protect main |

---

## Usage Examples

### Example 1: Full audit + apply for ianlintner/caretaker

```bash
export REPO="ianlintner/caretaker"
export ADMIN_PAT="ghp_..."  # PAT with repo + admin:repo scope

# Run audit (read-only, exit 0 = clean):
bash <(curl -fsSL https://raw.githubusercontent.com/ianlintner/caretaker/main/.github/scripts/repo-settings-audit.sh) "$REPO"

# Apply all settings (idempotent):
bash <(curl -fsSL https://raw.githubusercontent.com/ianlintner/caretaker/main/.github/scripts/repo-settings-apply.sh) "$REPO"
```

*(See Implementation Guide below for inline `gh api` commands when scripts are not yet published.)*

### Example 2: Audit only (no changes)

```bash
export REPO="ianlintner/caretaker-qa"
# Check what's wrong without touching anything:
AUDIT_ONLY=true bash repo-settings-apply.sh "$REPO"
```

### Example 3: Fix just the Actions permissions gap

```bash
gh api -X PUT /repos/ianlintner/caretaker/actions/permissions/workflow \
  --field default_workflow_permissions=write \
  --field can_approve_pull_request_reviews=true
```

---

## Implementation Guide

### Step 1 — Read current settings

```bash
REPO="owner/repo"

# A. Top-level settings
gh api /repos/$REPO --jq '{
  delete_branch_on_merge,
  allow_squash_merge,
  allow_merge_commit,
  allow_rebase_merge,
  allow_auto_merge
}'

# B. Actions workflow permissions
gh api /repos/$REPO/actions/permissions/workflow --jq '{
  default_workflow_permissions,
  can_approve_pull_request_reviews
}'

# C. Branch protection (returns 404 if none set)
gh api /repos/$REPO/branches/main/protection 2>/dev/null || echo "NO BRANCH PROTECTION"
```

### Step 2 — Apply top-level settings

```bash
REPO="owner/repo"

gh api -X PATCH /repos/$REPO \
  --field delete_branch_on_merge=true \
  --field allow_squash_merge=true \
  --field allow_merge_commit=false \
  --field allow_rebase_merge=false \
  --field allow_auto_merge=true
```

Verify:
```bash
gh api /repos/$REPO --jq '{delete_branch_on_merge, allow_squash_merge, allow_merge_commit, allow_rebase_merge, allow_auto_merge}'
# Expected: {"delete_branch_on_merge":true,"allow_squash_merge":true,"allow_merge_commit":false,"allow_rebase_merge":false,"allow_auto_merge":true}
```

### Step 3 — Apply Actions workflow permissions

```bash
REPO="owner/repo"

gh api -X PUT /repos/$REPO/actions/permissions/workflow \
  --field default_workflow_permissions=write \
  --field can_approve_pull_request_reviews=true
```

> **If this fails with 409 / org policy conflict**: The org has overridden this setting. You must update it at the org level: `gh api -X PUT /orgs/ORGNAME/actions/permissions/workflow ...` (requires org admin token).

Verify:
```bash
gh api /repos/$REPO/actions/permissions/workflow
# Expected: {"default_workflow_permissions":"write","can_approve_pull_request_reviews":true}
```

### Step 4 — Apply branch protection

Branch protection requires admin PAT. Use `$ADMIN_PAT` env var or `GH_TOKEN` set to your admin PAT.

```bash
REPO="owner/repo"
BRANCH="main"
# Adjust CI_CONTEXTS to match your actual required CI job names
CI_CONTEXTS='["lint","test"]'

GH_TOKEN="$ADMIN_PAT" gh api -X PUT /repos/$REPO/branches/$BRANCH/protection \
  --input - <<EOF
{
  "required_status_checks": {
    "strict": false,
    "contexts": $CI_CONTEXTS
  },
  "required_pull_request_reviews": {
    "dismiss_stale_reviews": false,
    "require_code_owner_reviews": false,
    "required_approving_review_count": 1
  },
  "enforce_admins": false,
  "restrictions": null,
  "required_linear_history": true,
  "allow_force_pushes": false,
  "allow_deletions": false
}
EOF
```

Verify:
```bash
GH_TOKEN="$ADMIN_PAT" gh api /repos/$REPO/branches/$BRANCH/protection \
  --jq '{
    required_status_checks: .required_status_checks.contexts,
    required_reviews: .required_pull_request_reviews.required_approving_review_count,
    enforce_admins: .enforce_admins.enabled,
    required_linear_history: .required_linear_history.enabled
  }'
```

### Step 5 — Run audit script (gap check)

Use this in CI as a gate to detect configuration drift:

```bash
#!/usr/bin/env bash
# repo-settings-audit.sh
# Usage: REPO="owner/repo" ./repo-settings-audit.sh
# Exit 0 = all settings match target; Exit 1 = gaps found

set -euo pipefail
REPO="${1:-${REPO:?'REPO env var required'}}"
GAPS=0

echo "=== Auditing $REPO ==="

# --- A. Top-level settings ---
settings=$(gh api /repos/$REPO --jq '{
  delete_branch_on_merge,
  allow_squash_merge,
  allow_merge_commit,
  allow_rebase_merge,
  allow_auto_merge
}')

check() {
  local key="$1" expected="$2"
  actual=$(echo "$settings" | jq -r ".$key")
  if [ "$actual" != "$expected" ]; then
    echo "  FAIL $key: got=$actual want=$expected"
    GAPS=$((GAPS + 1))
  else
    echo "  OK   $key=$actual"
  fi
}

echo "--- Repo top-level ---"
check delete_branch_on_merge true
check allow_squash_merge true
check allow_merge_commit false
check allow_rebase_merge false
check allow_auto_merge true

# --- B. Actions workflow permissions ---
echo "--- Actions workflow permissions ---"
wf=$(gh api /repos/$REPO/actions/permissions/workflow 2>/dev/null || echo '{}')
wf_perm=$(echo "$wf" | jq -r '.default_workflow_permissions // "unknown"')
wf_approve=$(echo "$wf" | jq -r '.can_approve_pull_request_reviews // "unknown"')

if [ "$wf_perm" != "write" ]; then
  echo "  FAIL default_workflow_permissions: got=$wf_perm want=write"
  GAPS=$((GAPS + 1))
else
  echo "  OK   default_workflow_permissions=write"
fi

if [ "$wf_approve" != "true" ]; then
  echo "  FAIL can_approve_pull_request_reviews: got=$wf_approve want=true"
  GAPS=$((GAPS + 1))
else
  echo "  OK   can_approve_pull_request_reviews=true"
fi

# --- C. Branch protection ---
echo "--- Branch protection (main) ---"
bp=$(gh api /repos/$REPO/branches/main/protection 2>/dev/null || echo '{}')
if [ "$bp" = '{}' ]; then
  echo "  FAIL branch protection: not configured"
  GAPS=$((GAPS + 1))
else
  lh=$(echo "$bp" | jq -r '.required_linear_history.enabled // false')
  fp=$(echo "$bp" | jq -r '.allow_force_pushes.enabled // true')
  ea=$(echo "$bp" | jq -r '.enforce_admins.enabled // true')
  pr_count=$(echo "$bp" | jq -r '.required_pull_request_reviews.required_approving_review_count // 0')

  [ "$lh" = "true" ] && echo "  OK   required_linear_history=true" || { echo "  FAIL required_linear_history: got=$lh want=true"; GAPS=$((GAPS + 1)); }
  [ "$fp" = "false" ] && echo "  OK   allow_force_pushes=false" || { echo "  FAIL allow_force_pushes: got=$fp want=false"; GAPS=$((GAPS + 1)); }
  [ "$ea" = "false" ] && echo "  OK   enforce_admins=false" || { echo "  FAIL enforce_admins: got=$ea want=false"; GAPS=$((GAPS + 1)); }
  [ "$pr_count" -ge 1 ] && echo "  OK   required_approving_review_count=$pr_count" || { echo "  FAIL required_approving_review_count: got=$pr_count want>=1"; GAPS=$((GAPS + 1)); }
fi

echo ""
if [ "$GAPS" -gt 0 ]; then
  echo "RESULT: $GAPS gap(s) found in $REPO. Run repo-settings-apply.sh to fix."
  exit 1
else
  echo "RESULT: All settings OK for $REPO."
  exit 0
fi
```

---

## Troubleshooting

### Issue: `can_approve_pull_request_reviews` PUT returns 200 but has no effect

**Cause**: Org-level policy overrides repo-level. The org setting takes precedence.

**Detect**:
```bash
gh api /orgs/ORGNAME/actions/permissions/workflow --jq '.can_approve_pull_request_reviews'
```

**Fix**: Update at org level (requires org admin token):
```bash
GH_TOKEN="$ORG_ADMIN_PAT" gh api -X PUT /orgs/ORGNAME/actions/permissions/workflow \
  --field default_workflow_permissions=write \
  --field can_approve_pull_request_reviews=true
```

### Issue: Branch protection PUT fails with 403

**Cause**: GITHUB_TOKEN in Actions workflows does not have admin access to branches.

**Fix**: Create a PAT with `repo` scope and store it as a secret (e.g., `ADMIN_PAT`). Then:
```bash
GH_TOKEN="${{ secrets.ADMIN_PAT }}" gh api -X PUT /repos/$REPO/branches/main/protection ...
```

### Issue: `allow_auto_merge` is true but `gh pr merge --auto` still fails

**Cause**: The PR's base branch has branch protection with required status checks, and at least one required check is still pending — this is expected. Auto-merge queues the merge for when checks complete.

**Cause 2**: caretaker's `merge()` call uses direct merge, not GitHub's auto-merge queue. Check whether pr_agent calls `merge_pull_request` (direct) or sets auto_merge flag via GraphQL.

**Workaround**: Confirm the target merge method in `pr_agent.agent.py::_merge_pr` and whether it uses the GraphQL `enablePullRequestAutoMerge` mutation vs direct REST merge.

### Issue: After applying branch protection, caretaker PRs get stuck (can't merge)

**Cause**: Branch protection requires a human review, but caretaker's auto-approve sets `event=APPROVE` via a bot identity — GitHub may not count a bot review as a valid approving review if `required_code_owner_reviews=true`.

**Fix**: Ensure `require_code_owner_reviews=false` AND ensure the GitHub App / token caretaker uses is not excluded from the list of valid reviewers.

### Issue: Caretaker opens duplicate update-releases-json PRs

**Cause**: `update-releases-json.yml` runs on release tag → fails to create PR (no GHA permission) → next release triggers again → another failure → many orphaned branches.

**Fix**: Apply Step 3 (Actions workflow permissions). Then manually close the orphaned branches.

### Issue: `enforce_admins: false` — does this mean admins bypass branch protection?

Yes. When `enforce_admins=false`, repository admins can force-push and merge without satisfying required checks. This is intentional for caretaker: it acts as an admin-level actor and needs to merge its own PRs without human review in some flows. If your security posture requires enforcing for admins, set `enforce_admins=true` but then ensure caretaker's merge flow goes through the PR approval path (not force-merge).

---

## Known settings that caretaker does NOT manage

The following settings are left at their current values by this skill:

- `has_issues`, `has_wiki`, `has_projects` — feature toggles, not automation-related
- Repository visibility (public/private)
- Repository secrets and environment variables
- Dependabot version update config (`.github/dependabot.yml`)
- CODEOWNERS file
- Org-level policies that override repo settings

---

## Related Skills

- **[caretaker-setup](./caretaker-setup.md)** — First-time caretaker setup in a repo (call this skill after setup)
- **[caretaker-debug](./caretaker-debug.md)** — Debug runtime issues; check repo settings as part of diagnosis
- **[caretaker-config](./caretaker-config.md)** — Configure caretaker agents and features

## Additional Resources

- [GitHub REST API: Repositories](https://docs.github.com/en/rest/repos/repos)
- [GitHub REST API: Branch Protection](https://docs.github.com/en/rest/branches/branch-protection)
- [GitHub REST API: Actions Permissions](https://docs.github.com/en/rest/actions/permissions)
- [Plan: Repo Settings Skill](../../docs/plans/2026-04-25-repo-settings-skill-plan.md)

## Version History

- v1.0 — Initial skill (2026-04-25): covers merge settings, Actions workflow permissions, branch protection
