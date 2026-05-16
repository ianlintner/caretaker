"""Webhook → graph projection helpers.

The dispatcher records webhook deliveries via Prometheus counters
(:func:`caretaker.observability.metrics.record_webhook_event`) but historically
the graph carried no record of *which* event signatures a repo saw. This
module bridges that gap by writing aggregated ``:WebhookKind`` nodes — one
per ``(repo, event_type, action)`` tuple — through the process-wide
:class:`~caretaker.graph.writer.GraphWriter`.

Cardinality is bounded: GitHub publishes ~80 event types and most carry a
small enum of actions, so even a busy repo lands in the low hundreds of
``:WebhookKind`` nodes. This is the trade-off the operator chose over
per-delivery nodes — full forensic trace lives in the ``:CausalEvent``
chain; ``:WebhookKind`` answers "which event sources are noisy or stale?".
"""

from __future__ import annotations

from datetime import UTC, datetime

from caretaker.graph.models import NodeType, RelType
from caretaker.graph.writer import get_writer


def _kind_id(repo: str, event: str, action: str | None) -> str:
    act = action or ""
    return f"webhook:{repo}:{event}:{act}"


def record_webhook_to_graph(
    *,
    repo: str,
    event_type: str,
    action: str | None,
    outcome: str,
) -> None:
    """Upsert a ``:WebhookKind`` node for this delivery's signature.

    Best-effort — when the writer is disabled (no Neo4j configured) this
    is a no-op. ``deliveries`` counter is not incremented here because the
    writer only supports ``MERGE … SET n += $props`` semantics, not atomic
    increments; rolling counters live in Prometheus. The graph node carries
    ``last_outcome`` / ``last_seen`` so the admin UI can highlight stale
    or failing signatures without joining against metrics.
    """
    if not repo or not event_type:
        return  # nothing meaningful to attribute
    writer = get_writer()
    now_iso = datetime.now(UTC).isoformat()
    node_id = _kind_id(repo, event_type, action)
    writer.record_node(
        NodeType.WEBHOOK_KIND,
        node_id,
        {
            "repo": repo,
            "event_type": event_type,
            "action": action or "",
            "last_outcome": outcome,
            "last_seen": now_iso,
        },
    )
    # Anchor to the :Repo node so subgraph queries scoped to a repo pick
    # up its webhook signatures in one hop.
    repo_id = f"repo:{repo}"
    writer.record_node(NodeType.REPO, repo_id, {"name": repo})
    writer.record_edge(
        NodeType.WEBHOOK_KIND,
        node_id,
        NodeType.REPO,
        repo_id,
        RelType.BELONGS_TO,
        {"observed_at": now_iso},
    )


def record_webhook_trigger(
    *,
    repo: str,
    event_type: str,
    action: str | None,
    target_label: str,
    target_id: str,
) -> None:
    """Add a ``TRIGGERED`` edge from a webhook signature to its routed target.

    ``target_label`` is one of ``"PR"``, ``"Issue"``, ``"Run"`` (the only
    targets the dispatcher currently routes to). ``target_id`` follows the
    same conventions used elsewhere (``pr:<n>``, ``issue:<n>``, ``run:<id>``)
    so the merge lands on the same node the builder maintains.
    """
    if not repo or not event_type or not target_id:
        return
    writer = get_writer()
    writer.record_edge(
        NodeType.WEBHOOK_KIND,
        _kind_id(repo, event_type, action),
        target_label,
        target_id,
        RelType.TRIGGERED,
        {"observed_at": datetime.now(UTC).isoformat()},
    )


__all__ = ["record_webhook_to_graph", "record_webhook_trigger"]
