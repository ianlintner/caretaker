# Caretaker Prometheus Metrics Plan

Targeted audit against `prometheus-metrics` paved-path SKILL (§1–§10) and the
implementation plan that closes the gaps. Companion to
`docs/memory-graph-plan.md` §6 (OpenTelemetry traces); metrics and traces
share the `service` label vocabulary and the exemplar `trace_id` bridge.

## 1. Audit findings (as of 2026-04-21)

| Area | Status | Evidence |
|------|--------|----------|
| `prometheus-client` / `prometheus-fastapi-instrumentator` in deps | **missing** | Neither appears in `pyproject.toml`. |
| HTTP `/metrics` endpoint on any service | **missing** | Only `/metrics/fanout` + `/metrics/storm` in `caretaker/admin/api.py` (admin dashboard JSON, unrelated to Prometheus). |
| RED metrics on MCP FastAPI app | **missing** | No instrumentation middleware wired in `mcp_backend/main.py`. |
| Outbound HTTP (`github_client`) client-side latency/error metrics | **missing** | `_request_with_client` logs and raises, never records metrics. |
| DB client metrics (Redis, Mongo, Neo4j) | **missing** | No hooks on Mongo (`state/backends`, `evolution/backends`), Neo4j (`graph/store.py`), or Redis (`state/dedup.py`). |
| Worker job metrics (`registry.run_one`, K8s launcher) | **missing** | `agent_span` emits OTel traces only; no counters. |
| Rate-limit cooldown gauge | **missing** | `rate_limit.py` mutates `RateLimitCooldown` state but does not expose a gauge. |
| K8s Service `prometheus.io/scrape` annotations | **missing** | None of the files in `infra/k8s/` carry scrape annotations or a `metrics`-named port. |
| `app.kubernetes.io/*` labels on Deployments/Services | **partial** | Current manifests use `app: caretaker-mcp` only. |

**Nothing today writes Prometheus samples.** Observability is limited to
structured logs + OpenTelemetry spans (M8).

## 2. Metrics to emit

All names follow OTel semantic conventions (§1.5). All histograms use the
§3 curated latency buckets (`0.005 … 10` seconds). Every series carries
`service="caretaker-mcp"` from `init_metrics(app, service)`.

| Metric | Type | Labels | Emitted from |
|--------|------|--------|--------------|
| `http_server_requests_total` | Counter | `service, http_method, http_route, http_status_code` | `prometheus-fastapi-instrumentator` (MCP) |
| `http_server_request_duration_seconds` | Histogram | `service, http_method, http_route, http_status_code` | `prometheus-fastapi-instrumentator` (MCP) |
| `caretaker_errors_total` | Counter | `kind` (`validation`/`upstream`/`internal`/`auth`/`ratelimit`) | explicit in `github_client` + FastAPI exception handler |
| `http_client_requests_total` | Counter | `service, peer_service, http_method, http_status_code` | `github_client/api.py::_request_with_client` |
| `http_client_request_duration_seconds` | Histogram | `service, peer_service, http_method, http_status_code` | same |
| `db_client_operations_total` | Counter | `service, db_system, db_operation, outcome` | `_timed_op` decorator on Mongo/Redis/Neo4j call sites |
| `db_client_operation_duration_seconds` | Histogram | `service, db_system, db_operation` | same |
| `worker_jobs_total` | Counter | `service, job, outcome` | `registry.run_one` (job = `agent.name`) + `k8s_worker.launcher.dispatch` (job = `"k8s-agent-worker"`) |
| `worker_job_duration_seconds` | Histogram | `service, job, outcome` | `registry.run_one` |
| `worker_queue_depth` | Gauge | `service, queue` | `k8s_worker.launcher.list_recent` sampler (queue = `caretaker-agent-worker`) |
| `caretaker_rate_limit_cooldown_seconds` | Gauge | `service, peer_service="github"` | `rate_limit.record_response_headers` / `record_rate_limit_response` |
| `app_info` | Gauge (=1) | `service, version, commit` | `observability/metrics.py` at `init_metrics` |

Route labels are templated — we rely on FastAPI's `request.scope["route"].path`
extraction. No user/tenant/email labels; `trace_id` flows only as histogram
exemplars (§6), never as a label.

## 3. Exposition surface

* `/metrics` served on a **separate ASGI app** bound to `:9090` inside the
  FastAPI `_lifespan` handler (new `uvicorn.Server` task started next to
  `init_tracing`). Cluster-internal only, no auth (§1.2).
* Default `Content-Type: text/plain; version=0.0.4; charset=utf-8` from
  `prometheus_client.make_asgi_app()`.
* MCP container exposes port `9090` named `metrics` alongside the existing
  `http` port. `Service` gains `prometheus.io/{scrape,port,path}` annotations.

## 4. Cardinality budget

Labels:
* `http_method`: ≤8.
* `http_status_code`: bucketed to actual codes used (≤15).
* `http_route`: ~20 templated routes.
* `peer_service`: `"github"` (single value today).
* `db_system`: `redis`, `mongo`, `neo4j` (3 values).
* `db_operation`: bounded enum of `get/set/delete/merge_node/merge_edge/run_cypher/…` (≤12).
* `job`: agent names (≤15).
* `outcome`: `success/failure/retry` (3).

Worst-case product ≪ 1000 series total (§4 hard budget). The new
`tests/test_metrics.py::test_cardinality_bound` asserts `< 1000` after a
warmup request.

## 5. Implementation order

1. `pyproject.toml` — add `prometheus-client>=0.20,<1` + `prometheus-fastapi-instrumentator>=7.0,<8` to `dependencies`.
2. New `src/caretaker/observability/metrics.py`: custom-metric registry,
   `init_metrics(app, service)`, `timed_op(...)` decorator, module-level
   counters/histograms/gauges, and a `metrics_asgi_app` + `start_metrics_server(port=9090)`.
3. Wire-up:
   * `mcp_backend/main.py::_lifespan` → `init_metrics(app, "caretaker-mcp")` + spawn `:9090` server.
   * `github_client/api.py::_request_with_client` → record HTTP-client counter + histogram.
   * `github_client/rate_limit.py` → set cooldown gauge on every observed header.
   * `graph/store.py::GraphStore.{merge_node,merge_edge,clear_all,ensure_indexes}` → `timed_op(db_system="neo4j", ...)`.
   * `state/backends/mongo_backend.py`, `evolution/backends/mongo_backend.py` → `timed_op(db_system="mongo", ...)` on each public method.
   * `state/dedup.py::RedisDedup` → `timed_op(db_system="redis", operation="setnx"|"get")`.
   * `registry.py::AgentRegistry.run_one` → `worker_jobs_total` + `worker_job_duration_seconds`.
   * `k8s_worker/launcher.py::K8sAgentLauncher.{dispatch,list_recent}` → `worker_jobs_total`, `worker_queue_depth`.
4. `tests/test_metrics.py` — asserts: RED metrics appear after a `TestClient.get("/health")`, histogram buckets match §3, `http_route` is templated (not resolved path), `len(REGISTRY.collect()) < 1000` post-warmup.
5. `infra/k8s/*.yaml` — add annotations + `app.kubernetes.io/*` labels + metrics port to MCP Service/Deployment.
6. `CHANGELOG.md` — add an `### Observability — Prometheus metrics + cluster scrape config` entry under `[Unreleased]` matching the M1/M8 voice.

## 6. Deviations from SKILL.md

None planned. If `prometheus-fastapi-instrumentator` emits its own default
buckets on unrelated histograms we override them with §3 edges. Redis/Neo4j
`db_system` values are straight from the OTel `db.system` attribute registry.
