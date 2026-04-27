# Fleet registry opt-in runbook

This runbook explains how to opt every child repository in `docs/fleet.yml`
into the caretaker **fleet registry**, so that the central admin dashboard
(`https://caretaker.cat-herding.net`) receives a heartbeat after every
orchestrator run and surfaces version, goal-health, error, and alert data
in real time.

The fleet registry is **independent** of the static `caretaker fleet lag`
audit driven by `docs/fleet.yml`. A repo can be in one without being in
the other; for full visibility a repo should be in both.

Authentication uses **OAuth2 client_credentials** against the shared
identity provider at `https://roauth2.cat-herding.net`. Each repo gets
its own dedicated client; heartbeats present a JWT bearer token with
scope `fleet:heartbeat` that the backend validates against the IdP's
JWKS. The legacy `CARETAKER_FLEET_SECRET` HMAC path was removed in
v0.20 — backends will reject any unauthenticated heartbeat with HTTP
401. Caretaker workflows fail-open at the emitter so a misconfigured
repo simply logs a warning and never breaks the run.

## Prerequisites (one-time, on the central caretaker repo)

Backend deployment must export `CARETAKER_OIDC_ISSUER_URL` (e.g.
`https://roauth2.cat-herding.net`) so the public heartbeat endpoint
loads the OIDC discovery document and JWKS at startup. Without it, the
endpoint returns HTTP 503. See `docs/admin-dashboard-activation.md`.

## Per-repo opt-in (apply to each child repo)

For each repo listed below:

1. **Register a per-repo OAuth2 client** at the IdP. The registration
   endpoint is open (no auth required) and returns a fresh
   `client_id` + `client_secret` pair scoped to `fleet:heartbeat`:

   ```sh
   curl -fsS -X POST https://roauth2.cat-herding.net/connect/register \
     -H 'Content-Type: application/json' \
     -d '{
       "client_name": "caretaker-fleet-<repo>",
       "redirect_uris": ["https://github.com/ianlintner/<repo>"],
       "grant_types": ["client_credentials"],
       "scope": "fleet:heartbeat"
     }'
   ```

   Save the returned `client_id` and `client_secret` — the secret is
   shown once and cannot be retrieved later.

2. **Add the GitHub Actions secrets and variables** for the repo. The
   workflow templates expect two secrets and two variables:

   ```sh
   gh secret set OAUTH2_CLIENT_ID -R ianlintner/<repo> --body '<client_id>'
   gh secret set OAUTH2_CLIENT_SECRET -R ianlintner/<repo> --body '<client_secret>'
   gh variable set OAUTH2_TOKEN_URL -R ianlintner/<repo> --body 'https://roauth2.cat-herding.net/oauth/token'
   gh variable set OAUTH2_SCOPE -R ianlintner/<repo> --body 'fleet:heartbeat'
   ```

3. **Edit `.github/maintainer/config.yml`.** Locate the
   `# fleet_registry:` block and uncomment it. Set `enabled: true` and
   confirm the OAuth2 sub-block matches the env-var names used by the
   workflow:

   ```yaml
   fleet_registry:
     enabled: true
     endpoint: https://caretaker.cat-herding.net/api/fleet/heartbeat
     timeout_seconds: 5.0
     include_full_summary: false
     oauth2:
       enabled: true
       client_id_env: OAUTH2_CLIENT_ID
       client_secret_env: OAUTH2_CLIENT_SECRET
       token_url_env: OAUTH2_TOKEN_URL
       scope_env: OAUTH2_SCOPE
       default_scope: "fleet:heartbeat"
       timeout_seconds: 10.0
   ```

   New child repos bootstrapped after v0.20 already ship with this
   block (see `setup-templates/templates/config-default.yml`); you only
   need to flip `enabled: false → true`.

4. **Confirm the workflow exports the OAuth2 env.** The shipped
   `.github/workflows/maintainer.yml` template exports the four OAuth2
   variables in the bootstrap-check, run, and self-heal `env:` blocks.
   Older copies (pre v0.20) export `CARETAKER_FLEET_SECRET` instead —
   re-sync from `setup-templates/templates/workflows/maintainer.yml`
   if needed.

5. **Verify locally** (optional but recommended):

   ```sh
   GITHUB_REPOSITORY=ianlintner/<repo> \
     OAUTH2_CLIENT_ID=<client_id> \
     OAUTH2_CLIENT_SECRET=<client_secret> \
     OAUTH2_TOKEN_URL=https://roauth2.cat-herding.net/oauth/token \
     OAUTH2_SCOPE=fleet:heartbeat \
     caretaker fleet register-self --config .github/maintainer/config.yml
   ```

   Expected output ends in `→ HTTP 200` and a JSON body containing
   `"ok": true`. The repo will then appear on the admin dashboard's
   Fleet page within seconds.

6. **Open a small PR** with the config diff and merge it. The next
   scheduled run will fire a real heartbeat; subsequent runs will
   continue to keep the registry warm.

## Repos to opt in

The list below mirrors `docs/fleet.yml` (12 repos). Apply the steps
above to each. Repo-specific caveats are noted inline.

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
   `fleet_store`, `fleet_oauth2`, and `fleet_backend_issuer` checks
   should all report `ok`.

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
`fleet_registry.enabled: false` in its `config.yml`. You may also
delete the GitHub Actions secrets/variables (`OAUTH2_CLIENT_ID`,
`OAUTH2_CLIENT_SECRET`, `OAUTH2_TOKEN_URL`, `OAUTH2_SCOPE`) and revoke
the OAuth2 client at the IdP. The next dashboard refresh will mark the
repo as `stale` (no recent heartbeats); after seven days without a
heartbeat the `ghosted` alert kind opens.

## Troubleshooting

- **HTTP 401 from heartbeat endpoint, `WWW-Authenticate: Bearer`.**
  The bearer token failed signature/issuer/expiry validation, or the
  Authorization header was missing. Confirm `OAUTH2_TOKEN_URL` points
  at `https://roauth2.cat-herding.net/oauth/token` and the
  `OAUTH2_CLIENT_ID`/`OAUTH2_CLIENT_SECRET` pair was issued by the
  same IdP.
- **HTTP 403 from heartbeat endpoint, `error="insufficient_scope"`.**
  The token was issued without `fleet:heartbeat` scope. Re-register
  the client (step 1 above) with the correct `scope` field, then
  rotate the secrets.
- **HTTP 503 from heartbeat endpoint.** The backend has not been
  configured with `CARETAKER_OIDC_ISSUER_URL` and refuses to verify
  tokens. Fix the deployment env, do not change child-repo config.
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
