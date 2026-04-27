# Plan: Caretaker Repo-Settings Skill

**Status:** Proposed
**Author:** drafted via opencode session 2026-04-25 (post v0.19.3 release)
**Owner:** TBD
**Related work:**
- `docs/plans/2026-04-25-qa-orchestration-test-plan.md`
- `.github/skills/caretaker-setup.md`
- caretaker#585 (check_run app_id mismatch)
- Session finding: update-releases-json.yml broke because "Allow GitHub Actions to create PRs" was OFF

---

## 1. Why this plan exists

During the v0.19.3 release session we encountered at least **four GitHub repository settings gaps** that caused noise, failures, or confusing behavior:

| Gap | Effect |
|-----|--------|
| "Allow GitHub Actions to create and approve pull requests" = OFF | `update-releases-json.yml` couldn't auto-create the releases.json PR; required manual intervention every release |
| `allow_auto_merge` = false | `gh pr merge --auto` doesn't work; CI-passing PRs must be merged manually or via a dedicated merge step |
| No branch protection on `main` | `caretaker/pr-readiness` check_run is informational only; nothing actually gates merges on CI green |
| Caretaker identity mismatch on check_runs | `pr_agent` tries to update a check_run created by a different GH App identity → 403 (#585) |

These gaps **differ between repos** (`ianlintner/caretaker` vs `ianlintner/caretaker-qa` vs any consumer repo), causing each new repo to rediscover the same configuration failures.

The fix is a **reusable skill** that:
1. Describes the *ideal target state* for all settings that affect caretaker operations.
2. Provides the `gh api` / `az cli` / REST API calls to apply those settings.
3. Can be invoked by Claude Code, Copilot, or a human to **audit and remediate** any managed repo in one pass.

---

## 2. Goals

1. **Single reference**: one markdown skill file (`caretaker-repo-settings.md`) that covers every setting that affects caretaker correctness.
2. **Idempotent apply script**: a shell script (or Python snippet) that reads current state and only patches the delta — running it twice leaves the repo unchanged.
3. **Audit mode**: same script can be run read-only to produce a gap report (exit 0 = all good, exit 1 = gaps found) suitable for CI checks.
4. **Multi-repo support**: parameterised by `OWNER/REPO`, runnable across the fleet (caretaker, caretaker-qa, and any consumer repo).
5. **Integrated into caretaker-setup skill**: `caretaker-setup.md` gets a "apply repo settings" step that calls this skill.

---

## 3. Non-goals

- We are **not** building a full GitHub admin dashboard.
- We are **not** configuring Org-level settings (those require admin-org token, out of scope for per-repo automation).
- We are **not** managing repository secrets or environment variables (covered by separate security guidance).

---

## 4. Target state specification

### 4.1 Repository top-level settings (`PATCH /repos/{owner}/{repo}`)

| Setting | Target value | Reason |
|---------|-------------|--------|
| `delete_branch_on_merge` | `true` | Stale branches are a noise source; caretaker's stale_agent assumes branches will be cleaned up |
| `allow_squash_merge` | `true` | Required for `merge_method: squash` in pr_agent config |
| `allow_merge_commit` | `false` | Disabling keeps history clean and avoids accidental non-squash merges |
| `allow_rebase_merge` | `false` | Same as above — one merge method across the board reduces confusion |
| `allow_auto_merge` | `true` | Enables `gh pr merge --auto` so pr_agent can queue a merge that fires when CI completes |
| `has_projects` | (leave current) | Not caretaker-managed |
| `has_wiki` | (leave current) | Not caretaker-managed |

### 4.2 Actions permissions (`PUT /repos/{owner}/{repo}/actions/permissions`)

| Setting | Target value | Reason |
|---------|-------------|--------|
| `allowed_actions` | `all` (or existing org policy) | Caretaker needs standard actions (`actions/checkout`, `actions/setup-python`, etc.) |

### 4.3 Actions default workflow permissions (`PUT /repos/{owner}/{repo}/actions/permissions/workflow`)

| Setting | Target value | Reason |
|---------|-------------|--------|
| `default_workflow_permissions` | `write` | Caretaker workflows need `contents: write` and `pull-requests: write` by default |
| `can_approve_pull_request_reviews` | `true` | **Critical**: without this, `update-releases-json.yml` cannot create PRs via `gh pr create`; caretaker auto-merge flows also require it |

### 4.4 Branch protection on default branch (`PUT /repos/{owner}/{repo}/branches/main/protection`)

Caretaker-ideal branch protection balances automation with safety:

| Field | Target value | Reason |
|-------|-------------|--------|
| `required_status_checks.strict` | `false` | Strict requires branch to be up-to-date before merge; caretaker squash-merges mean this would cause excessive rebases on queue drain |
| `required_status_checks.contexts` | `["build", "lint", "test"]` (per-repo, must match actual CI jobs) | Ensures CI must pass before merge; list must match actual job names |
| `required_pull_request_reviews.dismiss_stale_reviews` | `false` | Caretaker re-approves on new SHA via `last_approved_sha` tracking (v0.19.3+); stale dismissal + auto-approve would create approve-dismiss-approve loops |
| `required_pull_request_reviews.require_code_owner_reviews` | `false` | Not applicable to automated PRs |
| `required_pull_request_reviews.required_approving_review_count` | `1` | Require at least one approval (can be caretaker's own auto-approve) |
| `enforce_admins` | `false` | Admins bypass protection; caretaker acts as admin-level actor so enforcing for admins would block its merges |
| `restrictions` | `null` | No push restrictions — caretaker needs to push branches |
| `required_linear_history` | `true` | Squash-merge produces linear history; this enforces consistency |
| `allow_force_pushes` | `false` | Disallow force pushes to protect main |
| `allow_deletions` | `false` | Disallow deletion of main |

> **Note**: Branch protection via REST API requires a token with `repo` scope AND admin access to the repo. The GITHUB_TOKEN in GitHub Actions does not have admin access. A PAT with admin access (stored as secret `COPILOT_PAT` or `ADMIN_PAT`) must be used for the protection setup step.

### 4.5 caretaker-qa specific notes

The caretaker-qa repo uses `agentic.readiness=shadow` and all agent modes in shadow. The branch protection should match the same spec above but the `required_status_checks.contexts` list should match caretaker-qa's CI jobs (`build`, `lint`, `test` from `.github/workflows/ci.yml`).

---

## 5. Implementation plan

### Phase 1: Skill document (this PR)

Ship `.github/skills/caretaker-repo-settings.md` with:
- Target state tables (what the ideal settings are and why)
- `gh api` commands to read current state and apply each setting
- Audit script that exits 0/1 based on gap detection
- When-to-use guidance for Claude Code and Copilot
- Troubleshooting section (PAT scope, org policy conflicts)

**Deliverable**: Skill file merged, no code changes.

### Phase 2: DevOps agent integration (future PR)

Add `repo_settings_agent` (or extend `devops_agent`) that:
- On `workflow_dispatch` or scheduled run, calls GitHub REST to audit settings
- Emits a structured gap report into the caretaker tracking issue or as a check run annotation
- Optionally auto-fixes settings it has permission to change
- Escalates admin-required settings (branch protection) to humans

Key new APIs to add to `github_client/api.py`:
```python
async def get_repo_settings(self, owner: str, repo: str) -> dict: ...
async def patch_repo_settings(self, owner: str, repo: str, **settings) -> dict: ...
async def get_branch_protection(self, owner: str, repo: str, branch: str) -> dict | None: ...
async def put_branch_protection(self, owner: str, repo: str, branch: str, rules: dict) -> dict: ...
async def get_actions_workflow_permissions(self, owner: str, repo: str) -> dict: ...
async def put_actions_workflow_permissions(self, owner: str, repo: str, *, default_workflow_permissions: str, can_approve_pull_request_reviews: bool) -> None: ...
```

New config section in `config.py`:
```python
class RepoSettingsConfig(BaseModel):
    enabled: bool = False  # default off; must opt in
    audit_only: bool = True  # True = report gaps, False = also apply fixes
    branch_protection: bool = True  # audit/apply branch protection
    workflow_permissions: bool = True  # audit/apply GHA workflow permissions
    auto_merge: bool = True  # audit/apply allow_auto_merge
    delete_branch_on_merge: bool = True  # audit/apply delete_branch_on_merge
    required_status_checks: list[str] = []  # CI job names to require
```

### Phase 3: Fleet-wide enforcement (future PR)

Use `fleet` module to apply settings across all managed repos in one orchestrator run. Produces a fleet-wide settings health report.

---

## 6. Skill scope for Phase 1 (what the skill must cover)

The skill document must give Claude Code / Copilot everything needed to complete a **read-audit-apply** workflow in one conversation:

1. **Read current settings** — exact `gh api` invocations with field paths
2. **Compare to target** — logic / checklist for gap detection
3. **Apply settings** — exact `gh api` write invocations, in correct order (top-level repo → actions permissions → branch protection)
4. **Verify** — read-back after apply to confirm
5. **Known conflicts** — org-level policies that override repo-level (and how to detect them)
6. **Authentication notes** — which operations need admin PAT vs GITHUB_TOKEN

---

## 7. Acceptance criteria for Phase 1

- [ ] `.github/skills/caretaker-repo-settings.md` merged to main
- [ ] Skill covers all four settings gaps identified in this session
- [ ] Skill includes copy-paste `gh api` commands for audit and apply
- [ ] Skill includes audit exit-code script (0 = clean, 1 = gaps)
- [ ] Skill cross-references `caretaker-setup.md` and `caretaker-debug.md`
- [ ] PR is small and reviewable (<400 lines)

---

## 8. Open questions

1. **Branch protection required_status_checks**: which CI job names to require as mandatory?
   - For `ianlintner/caretaker`: `build`, `lint`, `test`, `doctor` — but `doctor` sometimes flakes.
   - Recommendation: start with `["lint", "test"]` as the minimum mandatory set; add more over time.
2. **`enforce_admins: false` vs `true`**: if caretaker acts as a GitHub App (not a personal token), does it bypass admin enforcement? Need to verify with the App identity.
3. **Org-level `can_approve_pull_request_reviews`**: if the org policy overrides the repo setting, the `PUT` call will succeed but the org policy takes precedence. The skill should document the org setting path and how to detect the conflict.
4. **`allow_auto_merge` vs caretaker merge timing**: with auto-merge enabled, a human or bot queuing `--auto` before CI finishes is now safe. But if caretaker calls `merge()` directly (not via GitHub's auto-merge queue), the `allow_auto_merge` setting is irrelevant. Document which path caretaker uses.
