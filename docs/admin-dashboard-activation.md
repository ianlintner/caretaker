# Admin dashboard activation (Phase F)

The admin dashboard (OIDC-gated read-only UI over caretaker state) ships
disabled by default (`CARETAKER_ADMIN_ENABLED=false`). This guide walks
through turning it on.

## Prerequisites

Infra that's already in place after the Phase C/D/E deploy:

- MCP backend deployed to `bigboy` AKS cluster, namespace `caretaker`
- Publicly reachable at <https://caretaker.cat-herding.net> (Istio VS)
- Redis deployed in-cluster at `redis.caretaker.svc.cluster.local:6379`
  (no auth; intra-cluster traffic only)

## Step 1 — register an OAuth client with `roauth2.cat-herding.net`

The admin dashboard uses your existing rust-oauth2-server as its OIDC
provider. Register a client with these settings:

- **Client name**: `caretaker-admin`
- **Redirect URI**: `https://caretaker.cat-herding.net/api/auth/callback`
- **Grant types**: `authorization_code`, `refresh_token`
- **Scopes**: `openid profile email`

You'll get a `client_id` and `client_secret` back.

## Step 2 — create a Neo4j AuraDB Free instance

1. Sign up at <https://neo4j.com/cloud/aura-free/>
2. Create a new AuraDB Free instance (EU or US region close to `nekoc` RG
   is fine — both work at <10ms to the admin UI)
3. Download the `.env` file — you'll need `NEO4J_URI` and
   `NEO4J_PASSWORD` (username is always `neo4j`)

## Step 3 — populate the `caretaker-admin-secrets` Secret

Generate a session secret first:

```bash
SESSION_SECRET=$(python -c 'import secrets; print(secrets.token_hex(32))')
```

Create the Secret in the `caretaker` namespace (values from steps 1 & 2):

```bash
kubectl -n caretaker create secret generic caretaker-admin-secrets \
  --from-literal=oidc-issuer-url="https://roauth2.cat-herding.net" \
  --from-literal=oidc-client-id="<client_id from step 1>" \
  --from-literal=oidc-client-secret="<client_secret from step 1>" \
  --from-literal=session-secret="$SESSION_SECRET" \
  --from-literal=public-base-url="https://caretaker.cat-herding.net" \
  --from-literal=allowed-emails="lintner.ian@gmail.com" \
  --from-literal=redis-url="redis://redis.caretaker.svc.cluster.local:6379/0" \
  --from-literal=neo4j-url="<NEO4J_URI from step 2>" \
  --from-literal=neo4j-auth="neo4j/<NEO4J_PASSWORD from step 2>" \
  --dry-run=client -o yaml | kubectl apply -f -
```

## Step 4 — flip `CARETAKER_ADMIN_ENABLED=true`

Edit `infra/k8s/caretaker-mcp-deployment.yaml`:

```yaml
- name: CARETAKER_ADMIN_ENABLED
  value: "true"   # was "false"
```

Commit and trigger the deploy workflow:

```bash
gh workflow run deploy-mcp.yml --ref main
```

## Step 5 — log in

Visit <https://caretaker.cat-herding.net> — you'll be redirected to the
rust-oauth2-server login, then back to the dashboard.

## Troubleshooting

- `/api/auth/login` returns **503**: the `CARETAKER_ADMIN_OIDC_ISSUER_URL`
  env var isn't set. Check the pod env: `kubectl -n caretaker exec
  deploy/caretaker-mcp -- env | grep ADMIN`.
- Redirect loop on login: `public_base_url` must match the host
  the browser is using (scheme + host, no trailing slash).
- `/api/graph/*` returns **503**: Neo4j isn't reachable. Check the
  Secret has `neo4j-url` / `neo4j-auth` set and the AuraDB instance is
  running.
- Session lost on every request: Redis isn't reachable. `kubectl -n
  caretaker exec deploy/caretaker-mcp -- redis-cli -u "$REDIS_URL"
  ping` should return PONG.
