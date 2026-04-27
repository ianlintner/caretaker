# Observability — End-to-End Tracing

Caretaker emits OpenTelemetry traces, Prometheus metrics, and
trace-id-annotated structured logs. This doc is the operator's
reference for finding a problem and pivoting between signals.

## What gets traced

End-to-end span tree for a single GitHub webhook delivery:

```
POST /webhooks/github                  ← caretaker-mcp (FastAPI)
└─ eventbus.publish caretaker:events   ← caretaker-mcp (Redis client)
   └─ … Redis Streams hop …            ← traceparent stamped on payload
      └─ eventbus.consume webhook      ← caretaker-mcp (consumer task)
         └─ invoke_agent <name>        ← gen_ai.agent.name=<pr|issue|…>
            ├─ HTTP api.github.com     ← httpx auto-instrumentation
            └─ chat <model>            ← gen_ai.* (manual)
               └─ HTTP api.anthropic   ← httpx auto-instrumentation
```

`/runs/{id}/trigger` (GitHub Actions-driven runs) follows the same
shape with a different root span name and a `run_trigger` payload kind.

## Cluster wiring

* **Collector**: `otel-collector.default.svc.cluster.local:4317` (gRPC).
  Tail-samples to Tempo: keeps errors, HTTP 5xx, anything >1s, and
  `caretaker-mcp` is on the always-keep service-name list. Everything
  else gets a 10% probabilistic baseline.
* **Backend**: Tempo at `tempo.default.svc:4317`. Browse via
  Grafana → Explore → Tempo.
* **Header scrubbing**: the collector strips `authorization`, `cookie`,
  `set-cookie` and truncates `url.full` to 1 KiB. Caretaker also
  redacts `Authorization` at the httpx instrumentor's request hook
  (defense in depth).

## Service identities

| `service.name`            | Process                                |
| ------------------------- | -------------------------------------- |
| `caretaker-mcp`           | FastAPI backend Deployment             |
| `caretaker-agent-worker`  | Per-dispatch k8s Job                   |
| `caretaker-cli`           | `caretaker run …` (local + GHA-driven) |

Filter in Tempo with `{ resource.service.name="caretaker-mcp" }`.

## Log → trace correlation

CLI / k8s_worker stdout log lines include the active trace context:

```
2026-04-27 14:02:11 INFO caretaker.pr_agent [trace_id=4bf92f3577b34da6a3ce929d0e0e4736 span_id=00f067aa0ba902b7] — opened PR #42
```

Paste the `trace_id` into Tempo search to jump to the trace. Empty
fields when no span is active (most CLI bootstrapping).

For run-stream logs (the SSE pipe to the admin dashboard) the
`trace_id` / `span_id` ride on `LogEntry.tags`, so the admin UI can
render a "view trace" link directly next to each log line.

## Filtering one webhook delivery

The consume span carries the GitHub `delivery_id` as a span attribute:

```
{ resource.service.name="caretaker-mcp" && span.caretaker.delivery_id="abc-123" }
```

That returns the full webhook → consume → agent → HTTP / LLM / Neo4j
span tree for that one delivery, even though the publisher and
consumer are different replicas.

## Disabling

Unset `OTEL_EXPORTER_OTLP_ENDPOINT` in the Deployment env. Every
caretaker observability helper is no-op when the endpoint is missing
or the `otel` extra is not installed; agents continue to run, log
lines continue to emit (without trace ids), no startup error.

## Adding spans in new code

Caretaker's auto-instrumentation already covers HTTP server (FastAPI),
HTTP client (httpx), Redis, and Python logging. Most new code needs
no manual spans.

When a manual span helps (a multi-step business operation, an LLM call
with custom attributes), reach for:

* `caretaker.observability.agent_span(agent_name, operation)` — agent
  invocations. Already wired in `AgentRegistry.run_one`.
* `caretaker.observability.llm_chat_span(...)` — LLM completions.
  Carries `gen_ai.*` semantic-convention attributes. Used by
  `AnthropicProvider.complete` and `LiteLLMProvider.complete*`.

For cross-process boundaries (anything that crosses Redis, a Job
boundary, or another HTTP hop you control on both ends), use:

* `caretaker.observability.inject_trace_context(payload)` — producer side.
* `caretaker.observability.extracted_context(payload)` — consumer side.

## Out of scope (today)

* OTLP **log** export. The cluster's logs pipeline currently exports
  only to the `debug` exporter. We enrich stdout with trace ids so
  operators can correlate manually; revisit OTLP log export when Loki
  lands.
* GitHub Actions runner-side spans for GHA-driven runs. The
  `/runs/{id}/trigger` cluster handler is fully traced; the runner
  that calls it would need its own bootstrap to make the runner-side
  HTTP call a child span. Separate workstream.
