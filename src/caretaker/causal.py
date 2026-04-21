"""Causal tokens embedded in caretaker markers (Sprint B3).

Every caretaker-authored issue or comment can carry a hidden ``caretaker:causal``
marker that identifies the workflow run + agent that authored it and, when
known, the parent causal token that led to this side-effect. Walking the
parent chain lets F3 (causal-chain audit UI, deferred) answer questions like:

  "This self-heal issue was filed — what sequence of runs produced it?"

Format
------

    <!-- caretaker:causal id=<id> source=<agent> [parent=<parent_id>] -->

``id`` is a stable identifier for the writing action. Callers should use
``make_causal_id(source)`` which builds ``run-<GITHUB_RUN_ID>-<source>`` when
available, falling back to a short uuid fragment for local/offline use.

Design notes
------------

The marker is emitted **alongside** existing markers rather than embedded in
them, so:
  * existing ``<!-- caretaker:xxx -->`` regexes keep matching
  * the dispatch-guard self-loop regex ``<!--\\s*caretaker:[a-z0-9:_-]+``
    already catches ``caretaker:causal`` — no workflow JS change needed
  * adding causal tokens to a write path is a single-line change
"""

from __future__ import annotations

import os
import re
import uuid

_CAUSAL_MARKER_RE = re.compile(
    r"<!--\s*caretaker:causal\s+"
    r"id=(?P<id>[A-Za-z0-9._:-]+)"
    r"(?:\s+source=(?P<source>[A-Za-z0-9._:-]+))?"
    r"(?:\s+parent=(?P<parent>[A-Za-z0-9._:-]+))?"
    r"\s*-->",
    re.IGNORECASE,
)


def make_causal_id(source: str, *, run_id: str | int | None = None) -> str:
    """Build a stable causal id for the current action.

    When ``run_id`` is supplied (or ``GITHUB_RUN_ID`` is set in the
    environment), returns ``run-<run_id>-<source>``; otherwise falls back to
    ``local-<uuid8>-<source>`` for offline / test use.
    """
    rid = str(run_id) if run_id is not None else os.environ.get("GITHUB_RUN_ID", "").strip()
    if rid:
        return f"run-{rid}-{source}"
    return f"local-{uuid.uuid4().hex[:8]}-{source}"


def make_causal_marker(
    source: str,
    *,
    run_id: str | int | None = None,
    parent: str | None = None,
    causal_id: str | None = None,
) -> str:
    """Build a ``caretaker:causal`` HTML-comment marker.

    Pass an explicit ``causal_id`` to stamp a caller-supplied id; otherwise a
    fresh id is generated via :func:`make_causal_id`.
    """
    cid = causal_id if causal_id is not None else make_causal_id(source, run_id=run_id)
    attrs = f"id={cid} source={source}"
    if parent:
        attrs += f" parent={parent}"
    return f"<!-- caretaker:causal {attrs} -->"


def extract_causal(body: str) -> dict[str, str] | None:
    """Return the first causal marker's fields from ``body``, or ``None``.

    Returned dict has keys ``id``, ``source`` (may be empty), and ``parent``
    (omitted when absent).
    """
    m = _CAUSAL_MARKER_RE.search(body or "")
    if not m:
        return None
    out: dict[str, str] = {"id": m.group("id"), "source": m.group("source") or ""}
    parent = m.group("parent")
    if parent:
        out["parent"] = parent
    return out


__all__ = [
    "extract_causal",
    "make_causal_id",
    "make_causal_marker",
]
