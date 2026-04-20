# Causal chains

Every caretaker-authored write — status updates, escalations, dispatch
comments, Charlie close comments, run-history snapshots — carries a
hidden HTML-comment marker that identifies the workflow run and agent
that authored it and, when known, the parent causal token that led to
this side-effect.

Walking the parent chain lets the admin dashboard answer questions like:

> This self-heal issue was filed — what sequence of runs produced it?

## Marker format

```html
<!-- caretaker:causal id=<id> source=<agent> [parent=<parent_id>] -->
```

- `id` is a stable identifier for the writing action. Callers build it
  with `caretaker.causal.make_causal_id(source)` which returns
  `run-<GITHUB_RUN_ID>-<source>` when available, falling back to a
  short uuid fragment for local/offline use.
- `source` is the producing agent (`devops`, `pr-agent:escalation`,
  `issue-agent:dispatch`, `charlie:close-duplicate-pr`,
  `state-tracker:run-history`, …).
- `parent` optionally points at the causal id of the event that
  triggered this one, stitching chains across runs.

The marker is emitted **alongside** existing markers rather than
embedded in them so the project's `caretaker:<kind>` regexes keep
matching and the dispatch-guard self-loop regex
(`<!--\s*caretaker:[a-z0-9:_-]+`) already catches it. Adding causal
tokens to a new write path is a one-line change.

## Write paths that carry causal tokens

As of the B3 expansion, these caretaker writes stamp causal markers:

- PR-agent escalation comments
- Issue-agent dispatch (assignment bodies + BUG_SIMPLE dispatch
  comments). Parent causal id is inherited from the source issue body.
- Charlie close comments: duplicate/stale issues and PRs
- Escalation-agent digest comments
- State-tracker orchestrator-state snapshot + rolling run-history
  comment

Status comments are intentionally excluded: they use strict body-
equality idempotency (`upsert_status_comment`), so a fresh
run-scoped marker each cycle would break the skip-if-unchanged path.

## Consuming causal data

The admin backend ships an in-memory `CausalEventStore` hydrated every
60 seconds from the watched repository's tracked issues, tracked PRs,
and tracking-issue comment stream. It exposes chain-walking primitives
and three read-only API endpoints:

- `GET /api/admin/causal?source=&offset=&limit=` — paged list, most
  recent first, optional source filter.
- `GET /api/admin/causal/{id}` — root-first parent chain for a single
  event. Returns `404` when the id is unknown. A cycle or a walk that
  hits `max_depth` sets `truncated=true`.
- `GET /api/admin/causal/{id}/descendants` — BFS descendants from a
  starting event.

Events also flow into the Neo4j graph as `CausalEvent` nodes with
`CAUSED_BY` edges back to their parent event, so graph queries can
traverse provenance alongside the existing `PR`, `Issue`, `Agent`, and
`Run` nodes.
