# Fleet Registry

Opt-in, per-repository heartbeat that lets an operator see every
caretaker-managed repository in one dashboard without running a
cross-org GitHub crawl.

## How it works

```
consumer repo                                central caretaker backend
┌──────────────────────┐        heartbeat    ┌────────────────────────────┐
│ caretaker run        │  ──── POST /api/── ▶│ /api/fleet/heartbeat       │
│  …end of run loop…   │   fleet/heartbeat   │   (unauthenticated,        │
│ emit_heartbeat()     │                     │    HMAC-verified if        │
└──────────────────────┘                     │    CARETAKER_FLEET_SECRET  │
                                             │    is set)                 │
                                             │                            │
                                             │ in-memory FleetRegistry    │
                                             │ ▲                          │
                                             │ │                          │
                                             │ │ read (OIDC-authed)       │
                                             │ │                          │
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
`fleet_registry.endpoint` URL.

## Configuring a consumer

Add to your repo's `.github/maintainer/config.yml`:

```yaml
fleet_registry:
  enabled: true
  endpoint: https://caretaker-admin.example.com/api/fleet/heartbeat
  # Optional. When set the emitter signs the payload with HMAC-SHA256
  # and forwards the digest in X-Caretaker-Signature. The backend
  # verifies before recording.
  secret_env: CARETAKER_FLEET_SECRET
  # Optional. Default False. When True the heartbeat body includes
  # the full RunSummary dump (every counter, goal metric, and error
  # list). Keep the default for public OSS fleets; enable only when
  # you control both sides.
  include_full_summary: false
  timeout_seconds: 5.0
```

If `CARETAKER_FLEET_SECRET` is exported in the workflow environment,
every heartbeat is signed:

```yaml
# .github/workflows/maintainer.yml
- name: Run caretaker
  env:
    GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
    CARETAKER_FLEET_SECRET: ${{ secrets.CARETAKER_FLEET_SECRET }}
    # …
  run: caretaker run --config .github/maintainer/config.yml
```

## Operating the backend

The heartbeat receiver is part of the same FastAPI app as the admin
dashboard. It's always mounted, regardless of `CARETAKER_ADMIN_ENABLED`,
so consumers can register even against a headless MCP deployment.

Set the shared secret on the backend environment to enforce HMAC:

```bash
export CARETAKER_FLEET_SECRET="…a-strong-shared-secret…"
```

Without a backend-side secret, the receiver accepts unsigned requests
(useful for private-network deployments and bootstrapping, *not* for
public internet-facing backends).

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

Unknown fields are accepted — the backend tolerates forward-compatible
additions to keep old backends working against newer emitters.

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

- **Opt-in, off by default.** No heartbeat without explicit config.
- **Fail-open.** Network, auth, or serialization errors are logged at
  `WARNING` and swallowed. A fleet-registry problem can never fail the
  orchestrator run loop.
- **In-memory store (for now).** The first cut persists only for the
  lifetime of the backend process. A future revision can plug the same
  `FleetRegistryStore` interface into the SQLite / Mongo backends that
  already back `state/` and `evolution/`.
- **No auto-discovery.** We chose the client-push model over a backend
  poll so operators without org-wide read PATs can still run a fleet
  dashboard.
