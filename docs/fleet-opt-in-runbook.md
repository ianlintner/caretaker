# Fleet registry opt-in runbook

This runbook explains how to opt every child repository in `docs/fleet.yml`
into the caretaker **fleet registry**, so that the central admin dashboard
(`https://caretaker.cat-herding.net`) receives a heartbeat after every
orchestrator run and surfaces version, goal-health, error, and alert data
in real time.

The fleet registry is **independent** of the static `caretaker fleet lag`
audit driven by `docs/fleet.yml`. A repo can be in one without being in
the other; for full visibility a repo should be in both.

## Prerequisites (one-time, on the central caretaker repo)

1. Pick (or rotate) a shared HMAC secret. Any high-entropy string works:

   ```sh
   openssl rand -hex 32
   ```

2. Store it as the caretaker repo's secret named `CARETAKER_FLEET_SECRET`
   (the dashboard backend already reads this env var to verify signatures).
   See `docs/admin-dashboard-activation.md` for the deployment-side wiring.

3. Hand the same secret out to every child repo as a GitHub Actions secret
   (also named `CARETAKER_FLEET_SECRET`). The runbook below assumes you
   have done this; if you skip it, heartbeats are still accepted but are
   unsigned (best-effort and not recommended for production).

## Per-repo opt-in (apply to each child repo)

For each repo listed below:

1. **Add the GitHub Actions secret.** Either via the GitHub UI
   (`Settings → Secrets and variables → Actions → New repository secret`)
   with name `CARETAKER_FLEET_SECRET`, or via the CLI:

   ```sh
   gh secret set CARETAKER_FLEET_SECRET --repo ianlintner/<repo>
   # paste the same secret used in step 1 of the prerequisites
   ```

2. **Edit `.github/maintainer/config.yml`.** Locate the
   `# fleet_registry:` block and uncomment it. Set `enabled: true` and
   confirm `endpoint:` points at production:

   ```yaml
   fleet_registry:
     enabled: true
     endpoint: https://caretaker.cat-herding.net/api/fleet/heartbeat
     secret_env: CARETAKER_FLEET_SECRET
     timeout_seconds: 5.0
     include_full_summary: false
   ```

   New child repos bootstrapped after this session already ship with
   this block (see `setup-templates/templates/config-default.yml`); you
   only need to flip `enabled: false → true`.

3. **Confirm the workflow exports the secret.** The shipped
   `.github/workflows/maintainer.yml` template already exports
   `CARETAKER_FLEET_SECRET` in the bootstrap-check, run, and self-heal
   `env:` blocks. Older copies (pre v0.19.x) may not — re-sync from
   `setup-templates/templates/workflows/maintainer.yml` if needed.

4. **Verify locally** (optional but recommended):

   ```sh
   GITHUB_REPOSITORY=ianlintner/<repo> \
     CARETAKER_FLEET_SECRET=<same-secret> \
     caretaker fleet register-self --config .github/maintainer/config.yml
   ```

   Expected output ends in `→ HTTP 200` and a JSON body containing
   `"ok": true`. The repo will then appear on the admin dashboard's
   Fleet page within seconds.

5. **Open a small PR** with the config diff and merge it. The next
   scheduled run will fire a real heartbeat; subsequent runs will
   continue to keep the registry warm.

## Repos to opt in

The list below mirrors `docs/fleet.yml` (12 repos). Apply the four
steps above to each. Repo-specific caveats are noted inline.

| # | Repo | Tier | Notes |
|---|------|------|-------|
| 1 | `ianlintner/caretaker-qa` | qa | Keeps fixture PRs open by design. Lag may exceed thresholds during fixture refreshes — known false positives, do not file lag issues. |
| 2 | `ianlintner/audio_engineer` | production | Workflow needs `workflows:write` permission for the upgrade agent (already merged 2026-04-23 in PR #70). |
| 3 | `ianlintner/kubernetes-apply-vscode` | production | None. |
| 4 | `ianlintner/Example-React-AI-Chat-App` | demo | Demo tier — heartbeat optional but recommended for full coverage. |
| 5 | `ianlintner/python_dsa` | production | None. |
| 6 | `ianlintner/flashcards` | production | None. |
| 7 | `ianlintner/rust-oauth2-server` | production | Long-running upgrade PRs #244 and #245. They will appear as `stuck` in the lag report; do not auto-close. |
| 8 | `ianlintner/portfolio` | demo | Demo tier. |
| 9 | `ianlintner/space-tycoon` | demo | Demo tier. |
| 10 | `ianlintner/tail_vapor` | production | None. |
| 11 | `ianlintner/AI-Pipeline` | production | None. |
| 12 | `ianlintner/algo_functional` | production | Re-bootstrapped at v0.19.1 (PR #13, 2026-04-23). Confirm the new template is in place before opt-in. |

## Verifying coverage from the admin dashboard

Once each repo has emitted at least one heartbeat:

1. Sign in at `https://caretaker.cat-herding.net`.
2. Visit **Fleet** — the table should list all 12 repos. Click any
   row to see the per-repo detail page (`/fleet/{owner}/{repo}`),
   including caretaker version, last 32 heartbeats, and goal-health
   trend.
3. Visit **Alerts** (`/alerts`) — open alerts, including
   `goal_health_regression`, `error_spike`, `ghosted`, and
   `scope_gap`, are listed with severity and a link back to the
   affected repo's detail page.
4. Visit **Health** (`/api/admin/health/doctor` returns JSON) — the
   `fleet_store` and `fleet_secret` checks should both report `ok`.

## Persistence

The dashboard now ships with a SQLite-backed registry. To enable
persistence across backend restarts, set the env var:

```sh
CARETAKER_FLEET_DB_PATH=/var/lib/caretaker/fleet-registry.db
```

The default path is `~/.local/state/caretaker/fleet-registry.db`. If the
env var is unset the registry remains in memory (the legacy default).
For tests the value `:memory:` opens an isolated in-memory SQLite
database.

## Rollback

To opt a repo *out* of the fleet registry, set
`fleet_registry.enabled: false` in its `config.yml` and remove the
`CARETAKER_FLEET_SECRET` repository secret. The next dashboard refresh
will mark the repo as `stale` (no recent heartbeats); after seven days
without a heartbeat the `ghosted` alert kind opens.

## Troubleshooting

- **HTTP 401/403 from heartbeat endpoint.** The secret on the dashboard
  side does not match the secret on the child repo. Rotate both.
- **HTTP 422 from heartbeat endpoint.** Heartbeat payload failed
  validation. Run `caretaker fleet register-self --config <path>`
  locally to capture the raw error body.
- **Repo missing from dashboard despite green workflow.** Check that
  `fleet_registry.enabled: true` *and* `endpoint:` are set, then look
  at the workflow run log for warning lines from the `fleet.emitter`
  logger. Heartbeat failures are intentionally fail-open and log only.
- **Dashboard shows correct repo but `last_seen` is hours old.** The
  scheduled cron is set per-repo in `.github/workflows/maintainer.yml`
  (`schedule.cron`). Heartbeats only fire on real orchestrator runs.
