# Flux GitOps for caretaker

Mirrors the pattern used by `Example-React-AI-Chat-App`. Flux reconciles
the manifests under `k8s/apps/<name>/` into the target cluster on every
`main` commit; the reconciliation is driven by the Flux `Kustomization`
CRs declared under `k8s/flux/clusters/<cluster>/`.

## Layout

```
k8s/
├── apps/
│   ├── caretaker/                 — main bundle (Redis, Neo4j, MCP, agent-worker)
│   │   ├── kustomization.yaml
│   │   └── namespace.yaml
│   └── caretaker-ingress/         — Istio VirtualService (default ns)
│       └── kustomization.yaml
└── flux/
    ├── README.md                  — this file
    └── clusters/
        └── bigboy/
            └── caretaker.yaml     — two Kustomization CRs (app + ingress)
```

The underlying resource YAML stays in `infra/k8s/`. The `k8s/apps/*/kustomization.yaml` files reference those paths so a hand-apply (`kubectl apply -f infra/k8s/`) and Flux GitOps agree on a single source of truth.

## Target

| Cluster | Context | Namespace | Domain |
|---------|---------|-----------|--------|
| `bigboy` | `bigboy` | `caretaker` (app) / `default` (ingress) | `caretaker.cat-herding.net` |

## One-time bootstrap

The cluster already runs the Azure-managed Flux extension (`fluxconfig-agent` + `fluxconfig-controller`). Onboard the caretaker repo with `az k8s-configuration flux create` (or apply a `FluxConfig` CR directly — see the existing `chat-app` FluxConfig for the template):

```sh
az k8s-configuration flux create \
  --name caretaker \
  --namespace flux-system \
  --cluster-name <aks-name> \
  --resource-group <rg> \
  --cluster-type managedClusters \
  --scope cluster \
  --url https://github.com/ianlintner/caretaker.git \
  --branch main \
  --interval 1m \
  --kustomization \
    name=caretaker \
    path=k8s/flux/clusters/bigboy \
    prune=true \
    sync_interval=1m \
    timeout=10m
```

That `FluxConfig` provisions a `GitRepository` source named `caretaker` in the `flux-system` namespace. The `Kustomization` CRs in `k8s/flux/clusters/bigboy/caretaker.yaml` reference that source and reconcile the app bundles.

Caretaker is a public repo, so no auth secret is needed. If you later make it private, pass `--https-user`, `--https-key` (PAT), or `--ssh-private-key` to `az k8s-configuration flux create`.

## Verify after bootstrap

```sh
flux get sources git -n flux-system caretaker
flux get kustomizations -n flux-system caretaker caretaker-ingress
kubectl get pods -n caretaker
kubectl get virtualservice -n default caretaker-virtualservice
```

Expect both Kustomizations `Ready=True`, all pods `Running`, VirtualService advertised on `caretaker.cat-herding.net`.

## Roll back

GitOps roll back: revert the offending commit on `main`; Flux reconciles to the prior state on the next 1-minute sync. For an emergency stop:

```sh
flux suspend kustomization -n flux-system caretaker
```

Resume with `flux resume kustomization -n flux-system caretaker`.

## Adding a new cluster

1. Add `k8s/flux/clusters/<cluster>/caretaker.yaml` mirroring `bigboy/caretaker.yaml`.
2. Apply a new `FluxConfig` against the new cluster's context with `path=k8s/flux/clusters/<cluster>`.

Keep per-cluster deltas out of `k8s/apps/` — prefer per-cluster kustomize overlays under `k8s/apps/caretaker/overlays/<cluster>/` and point the new cluster's `Kustomization` at the overlay.
