"""Causal-chain data model and traversal helpers (Sprint F3).

B3 sprinkles ``<!-- caretaker:causal id=... source=... [parent=...] -->`` markers
into every caretaker-authored comment / issue body. F3 harvests those markers
into :class:`CausalEvent` objects and exposes chain-walking primitives so the
admin dashboard can answer:

    "This self-heal issue was filed — what sequence of runs produced it?"

The types here are pure data; the ingestion + API surface lives in
:mod:`caretaker.admin.causal_store` and :mod:`caretaker.admin.api`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from caretaker.causal import extract_causal
from caretaker.observability import current_span_ids

if TYPE_CHECKING:
    from datetime import datetime

_RUN_ID_IN_CAUSAL_ID_RE = re.compile(r"^run-([^-]+)-")


@dataclass(frozen=True)
class CausalEventRef:
    """Which GitHub object a causal event was observed on."""

    kind: str  # "issue" | "pr" | "comment"
    number: int | None = None
    comment_id: int | None = None
    owner: str = ""
    repo: str = ""


@dataclass
class CausalEvent:
    """One causal marker lifted out of a GitHub body/comment.

    M8 of the memory-graph plan adds optional OTel span provenance
    (``span_id`` + ``parent_span_id``) so a ``:CausalEvent`` node can
    cross-link to the trace backend (Phoenix / Datadog / LangSmith)
    with a one-hop join. These fields default to ``None`` and are
    populated automatically from the active OTel span via
    :func:`caretaker.observability.current_span_ids` when the helper
    extractors in this module are called inside a span context.
    """

    id: str
    source: str
    parent_id: str | None
    ref: CausalEventRef
    run_id: str | None = None
    title: str = ""
    observed_at: datetime | None = None
    span_id: str | None = None
    parent_span_id: str | None = None


def parse_run_id(causal_id: str) -> str | None:
    """Parse ``run-<id>-<source>`` → ``<id>``; return ``None`` if no match."""
    m = _RUN_ID_IN_CAUSAL_ID_RE.match(causal_id or "")
    return m.group(1) if m else None


def extract_from_body(
    body: str,
    *,
    ref: CausalEventRef,
    title: str = "",
    observed_at: datetime | None = None,
) -> CausalEvent | None:
    """Extract the first causal marker from ``body`` → :class:`CausalEvent`.

    Returns ``None`` when the body has no marker.
    """
    fields = extract_causal(body or "")
    if fields is None:
        return None
    cid = fields["id"]
    span_id, parent_span_id = current_span_ids()
    return CausalEvent(
        id=cid,
        source=fields.get("source", ""),
        parent_id=fields.get("parent"),
        ref=ref,
        run_id=parse_run_id(cid),
        title=title,
        observed_at=observed_at,
        span_id=span_id,
        parent_span_id=parent_span_id,
    )


@dataclass
class Chain:
    """A root-to-leaf chain of :class:`CausalEvent`, oldest first."""

    events: list[CausalEvent] = field(default_factory=list)
    truncated: bool = False  # True if walk hit max_depth or a cycle


def walk_chain(
    index: dict[str, CausalEvent],
    start_id: str,
    *,
    max_depth: int = 50,
) -> Chain:
    """Walk the parent chain of ``start_id`` root-first.

    The returned :class:`Chain` lists events from the earliest known ancestor
    down to ``start_id``. When a parent id points outside ``index`` (unknown
    ancestor) we stop at the deepest known event. A cycle or hitting
    ``max_depth`` sets ``truncated=True``.
    """
    if start_id not in index:
        return Chain(events=[], truncated=False)

    collected: list[CausalEvent] = []
    seen: set[str] = set()
    node: CausalEvent | None = index[start_id]
    truncated = False
    while node is not None:
        if node.id in seen:
            truncated = True
            break
        if len(collected) >= max_depth:
            truncated = True
            break
        collected.append(node)
        seen.add(node.id)
        parent = node.parent_id
        node = index.get(parent) if parent else None

    collected.reverse()  # root first
    return Chain(events=collected, truncated=truncated)


def descendants(
    index: dict[str, CausalEvent],
    start_id: str,
    *,
    max_depth: int = 50,
) -> list[CausalEvent]:
    """Return every event whose chain passes through ``start_id``.

    Order is breadth-first from ``start_id``'s direct children outward.
    """
    if start_id not in index:
        return []

    children_by_parent: dict[str, list[CausalEvent]] = {}
    for ev in index.values():
        if ev.parent_id:
            children_by_parent.setdefault(ev.parent_id, []).append(ev)

    out: list[CausalEvent] = []
    seen: set[str] = {start_id}
    frontier = list(children_by_parent.get(start_id, []))
    depth = 0
    while frontier and depth < max_depth:
        next_frontier: list[CausalEvent] = []
        for ev in frontier:
            if ev.id in seen:
                continue
            seen.add(ev.id)
            out.append(ev)
            next_frontier.extend(children_by_parent.get(ev.id, []))
        frontier = next_frontier
        depth += 1
    return out


__all__ = [
    "CausalEvent",
    "CausalEventRef",
    "Chain",
    "descendants",
    "extract_from_body",
    "parse_run_id",
    "walk_chain",
]
