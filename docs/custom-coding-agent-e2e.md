# Custom coding agent — end-to-end verification

After `deploy-mcp.yml` runs post-#429 you should be able to fire a real
coding task from the admin UI and watch a Kubernetes Job spawn, run,
exit. This runbook walks through the verification.

## Pre-flight

1. Confirm deploy succeeded (`gh run list --workflow deploy-mcp.yml --limit 1`).
2. `kubectl` context points at the cluster:
   ```bash
   az aks get-credentials \
     --resource-group "$CARETAKER_AKS_RESOURCE_GROUP" \
     --name "$CARETAKER_AKS_CLUSTER_NAME"
   ```
3. Resources created:
   ```bash
   kubectl -n caretaker get sa caretaker-mcp caretaker-agent-worker
   kubectl -n caretaker get role caretaker-agent-worker-job-creator
   kubectl -n caretaker get rolebinding caretaker-agent-worker-binding
   kubectl -n caretaker get job caretaker-agent-worker-template
   kubectl -n caretaker get deploy caretaker-mcp -o jsonpath='{.spec.template.spec.serviceAccountName}{"\n"}'
   # → expected: caretaker-mcp
   ```
4. MCP pod healthy:
   ```bash
   kubectl -n caretaker get pods -l app=caretaker-mcp
   kubectl -n caretaker logs deploy/caretaker-mcp | grep -i "fleet\|k8s agent-worker\|admin"
   # Expect: "K8s agent-worker admin API enabled (namespace=caretaker)"
   ```
   If the log line is missing, confirm
   `executor.k8s_worker.enabled: true` is actually in the config the
   MCP pod reads (env `CARETAKER_CONFIG_PATH` → default
   `.github/maintainer/config.yml`).

## Enable the feature

Add to `.github/maintainer/config.yml` on the caretaker repo (or
wherever the MCP pod mounts its config):

```yaml
executor:
  k8s_worker:
    enabled: true
    namespace: caretaker
    image: gabby.azurecr.io/caretaker-agent-worker:latest
    service_account: caretaker-agent-worker
    dedupe_ttl_seconds: 900
```

Build the worker image (reuses `Dockerfile.mcp` with a different entrypoint
— Phase 4 adds a dedicated Dockerfile.agent; until then the MCP image
works because it ships the same `caretaker` CLI):

```bash
ACR=$(gh variable get CARETAKER_ACR_NAME --repo ianlintner/caretaker)
docker buildx build -f Dockerfile.mcp \
  --platform linux/arm64 \
  --tag "${ACR}.azurecr.io/caretaker-agent-worker:latest" \
  --push .
```

## Fire a task

### Via the admin UI (recommended)

1. Open `https://<CARETAKER_ADMIN_PUBLIC_BASE_URL>/admin/` in a browser.
2. Sign in through OIDC.
3. Navigate to the Fleet / Agents page (Phase 4 adds a dedicated
   dashboard; until then use the API directly below).

### Via curl (requires the session cookie)

```bash
# Grab your session cookie from the browser after OIDC login; alternatively
# run the MCP locally with OIDC disabled for smoke tests.
COOKIE="session=..."

curl -X POST "https://<admin-base-url>/api/admin/agent-tasks" \
  -H "Cookie: $COOKIE" \
  -H "Content-Type: application/json" \
  -d '{
    "repo": "ianlintner/demo",
    "issue_number": 1,
    "task_type": "LINT_FAILURE"
  }' | jq .
```

Expected response (HTTP 200):

```json
{
  "job_name": "caretaker-agent-demo-1-lint-failure-abcd1234",
  "namespace": "caretaker",
  "repo": "ianlintner/demo",
  "issue_number": 1,
  "task_type": "LINT_FAILURE",
  "created_at": "2026-04-21T03:24:15+00:00",
  "deduped": false
}
```

## Observe

```bash
# Job created:
kubectl -n caretaker get jobs -l app=caretaker-agent-worker --sort-by=.metadata.creationTimestamp

# Pod logs (follow until exit):
kubectl -n caretaker logs -l job-name=caretaker-agent-demo-1-lint-failure-abcd1234 -f

# If the pod errors, check RBAC:
kubectl -n caretaker auth can-i create jobs --as=system:serviceaccount:caretaker:caretaker-mcp
# → yes
```

## Verify dedupe

Re-fire the same POST within 15 minutes (default `dedupe_ttl_seconds`):

```bash
curl -X POST "https://<admin-base-url>/api/admin/agent-tasks" \
  -H "Cookie: $COOKIE" \
  -H "Content-Type: application/json" \
  -d '{"repo":"ianlintner/demo","issue_number":1,"task_type":"LINT_FAILURE"}' | jq .
```

Expected: same `job_name`, `deduped: true`.

## Teardown / reset

```bash
kubectl -n caretaker delete job -l app=caretaker-agent-worker
# Redis dedupe keys clear automatically at TTL; or:
kubectl -n caretaker exec -it statefulset/redis -- redis-cli DEL \
  "caretaker:agent-dispatch:ianlintner/demo#1:LINT_FAILURE"
```

## Known gotchas

- **Image pull failure** — the worker image defaults to
  `CARETAKER_IMAGE_NOT_CONFIGURED` if `config.image` isn't set. Set a
  real ACR ref in the config.
- **`kubernetes` package missing** — `POST /api/admin/agent-tasks`
  returns 400 `Kubernetes worker requires the kubernetes package`.
  Rebuild the MCP image with the `k8s-worker` extras group.
- **403 from BatchV1Api** — happens if the Deployment's
  `serviceAccountName` isn't `caretaker-mcp`. Re-run `deploy-mcp.yml`
  after #429 is on main.
- **Template Job never runs** — the template has no `suspend: true`
  and no schedule; Kubernetes may try to run it immediately.
  Acceptable on a fresh cluster because the pod has the bogus
  `MUST_BE_OVERRIDDEN_BY_MCP_BACKEND` env and will exit; Phase 4 will
  add `metadata.annotations["caretaker.io/template"] = "true"` +
  `spec.suspend: true` so the template doesn't actually run.
