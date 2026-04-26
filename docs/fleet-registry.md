# Fleet Registry

Opt-in, per-repository heartbeat that lets an operator see every
caretaker-managed repository in one dashboard without running a
cross-org GitHub crawl.

## How it works

```
consumer repo                                central caretaker backend
┌──────────────────────┐                     ┌────────────────────────────┐
│ caretaker run        │   client_credentials│ /oauth/token (IdP)         │
│  …end of run loop…   │ ──────────────────▶ │  → JWT, scope=fleet:heartbeat
│ emit_heartbeat()     │                     │                            │
│  • OAuth2 token      │                     │                            │
│  • Bearer-signed POST│   POST /api/fleet/  │ /api/fleet/heartbeat       │
│                      │ ──────────────────▶ │   (Bearer JWT validated    │
└──────────────────────┘   heartbeat         │    via JWKS)               │
                                              │ in-memory FleetRegistry    │
                                              │ ▲                          │
                                              │ │                          │
                                              │ │ read (OIDC session-      │
                                              │ │ authed)                  │
                                              │ /api/admin/fleet           │
                                              │ /api/admin/fleet/summary   │
                                              │ /api/admin/fleet/{repo}    │
                                              └────────────────────────────┘
                                                        ▲
                                                        │
                                                   admin dashboard
                                                     /fleet route
```

The feature is **off by default**. Caretaker never phones home unless
the consumer sets both `fleet_registry.enabled: true` and a concrete
`fleet_registry.endpoint` URL, and provisions OAuth2 client credentials.

## Configuring a consumer

Add to your repo's `.github/maintainer/config.yml`:

```yaml
fleet_registry:
  enabled: true
  endpoint: https://caretaker.cat-herding.net/api/fleet/heartbeat
  # Optional. Default False. When True the heartbeat body includes
  # the full RunSummary dump (every counter, goal metric, and error
  # list). Keep the default for public OSS fleets; enable only when
  # you control both sides.
  include_full_summary: false
  timeout_seconds: 5.0
  oauth2:
    enabled: true
    client_id_env: OAUTH2_CLIENT_ID
    client_secret_env: OAUTH2_CLIENT_SECRET
    token_url_env: OAUTH2_TOKEN_URL
    scope_env: OAUTH2_SCOPE
    default_scope: "fleet:heartbeat"
    timeout_seconds: 10.0
```

Provision the OAuth2 secrets/variables in the GitHub repo (see
`docs/fleet-opt-in-runbook.md`). The workflow then exports them to the
caretaker process:

```yaml
# .github/workflows/maintainer.yml
- name: Run caretaker
  env:
    GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
    OAUTH2_CLIENT_ID: ${{ secrets.OAUTH2_CLIENT_ID }}
    OAUTH2_CLIENT_SECRET: ${{ secrets.OAUTH2_CLIENT_SECRET }}
    OAUTH2_TOKEN_URL: ${{ vars.OAUTH2_TOKEN_URL }}
    OAUTH2_SCOPE: ${{ vars.OAUTH2_SCOPE }}
    # …
  run: caretaker run --config .github/maintainer/config.yml
```

## Operating the backend

The heartbeat receiver is part of the same FastAPI app as the admin
dashboard. It's always mounted, regardless of `CARETAKER_ADMIN_ENABLED`,
so consumers can register even against a headless MCP deployment.

Set the OIDC issuer URL on the backend environment so the receiver
loads JWKS at startup and validates incoming bearer tokens:

```bash
export CARETAKER_OIDC_ISSUER_URL="https://roauth2.cat-herding.net"
```

If the issuer URL is missing the public heartbeat endpoint returns
HTTP 503 — fail-closed by design. Tokens must be signed by the
issuer, not expired, and carry the `fleet:heartbeat` scope; otherwise
the response is HTTP 401 (`WWW-Authenticate: Bearer`) or HTTP 403
(`error="insufficient_scope"`).

## Payload shape

```json
{
  "schema_version": 1,
  "repo": "ianlintner/demo",
  "caretaker_version": "0.11.0",
  "run_at": "2026-04-20T23:00:00+00:00",
  "mode": "full",
  "enabled_agents": ["pr_agent", "issue_agent", "upgrade_agent"],
  "goal_health": 0.82,
  "error_count": 0,
  "counters": {
    "prs_monitored": 3,
    "prs_merged": 1,
    "issues_triaged": 2,
    "…": "20 curated counters from RunSummary"
  }
}
```

The receiver also stamps the validated `client_id` from the bearer
token onto each stored heartbeat (`authenticated_client_id`) for
audit. Unknown fields are accepted — the backend tolerates
forward-compatible additions to keep old backends working against
newer emitters.

## Dashboard

The admin dashboard gets a new **Fleet** route under the main nav with:

- Four StatPanels (registered repos, stale >7d, version mix, opt-in
  status).
- A hairline DataTable of every known client (repo, version, last
  mode, goal health, error count, agent count, heartbeat count, last
  seen).

Stale clients (no heartbeat in ≥7 days) are flagged so you can chase
down a silent consumer.

## Design notes

- **Opt-in, off by default.** No heartbeat without explicit config and
  OAuth2 credentials.
- **Fail-open at the emitter.** Network, auth, or serialization errors
  on the consumer side are logged at `WARNING` and swallowed. A
  fleet-registry problem can never fail the orchestrator run loop.
- **Fail-closed at the receiver.** Without `CARETAKER_OIDC_ISSUER_URL`
  or with an invalid token, the heartbeat endpoint refuses the
  request. Operators should monitor 401/503 rates as a signal of
  misconfigured consumers.
- **Pluggable persistence.** The default in-memory store keeps the
  registry for the lifetime of the backend process — useful for
  ephemeral test deployments. Set `CARETAKER_FLEET_DB_PATH` to a
  writable filesystem path (default
  `~/.local/state/caretaker/fleet-registry.db`) to enable the
  SQLite-backed `FleetRegistryStore`, which survives pod restarts and
  is the recommended setting for the production
  `caretaker.cat-herding.net` deployment. Both implementations honor
  the same async interface.
- **No auto-discovery.** We chose the client-push model over a backend
  poll so operators without org-wide read PATs can still run a fleet
  dashboard.
