"""Cross-run memory retriever — T-E2 of the 2026-Q2 agentic plan.

Every dispatch publishes one :class:`~caretaker.memory.core.AgentCoreMemory`
node via the graph writer, but before this module nothing ever read those
nodes back. The Phase 2 LLM decision migrations (starting with PR readiness)
benefit enormously from "here's what happened last time you saw a PR that
looked like this" — so we expose a small retriever that:

1. Reads ``:AgentCoreMemory`` nodes from Neo4j (or any object with a
   compatible ``list_nodes_with_properties`` coroutine — tests wire a
   fake), filtered by ``agent`` and optionally ``repo_slug``.
2. Ranks candidates by similarity to a ``query_text``. If both the query
   and stored rows have a ``summary_embedding`` vector and an
   :class:`~caretaker.memory.embeddings.Embedder` is wired, we rank by
   cosine similarity. Otherwise we fall back to Jaccard word-overlap on
   the ``summary`` strings — no embeddings required, no extra deps.
3. Caps the return to ``max_hits`` (default 3).
4. Renders a stable markdown block via :meth:`format_for_prompt` that is
   budget-aware: it truncates long summaries so the rendered block stays
   under ``max_tokens`` (estimated at 4 chars per token, conservative).

The retriever is deliberately graph-store agnostic: callers pass in any
object that exposes ``list_nodes_with_properties(label, where=, params=)``
— :class:`caretaker.graph.store.GraphStore` already does — and the
module never imports Neo4j directly. That keeps the shadow-mode unit
tests fast and lets the retriever stay a library, not a component.
"""

from __future__ import annotations

import logging
import math
from typing import TYPE_CHECKING, Any, Protocol

from pydantic import BaseModel

if TYPE_CHECKING:
    from caretaker.memory.embeddings import Embedder

logger = logging.getLogger(__name__)


# ── Public types ─────────────────────────────────────────────────────────


class CoreMemoryHit(BaseModel):
    """One :class:`~caretaker.memory.core.AgentCoreMemory` ranked for retrieval.

    Mirrors the minimum set of fields the readiness prompt actually needs
    to surface prior context: who the agent was, a short summary of the
    prior run, the outcome, a similarity score, when it happened, and —
    where known — the PR / issue number the run was acting on. Stored
    memories may be missing ``outcome`` (older rows written before T-E2
    upgraded the schema) — in that case we surface ``"unknown"`` so the
    prompt stays readable.
    """

    agent: str
    summary: str
    outcome: str
    similarity: float
    run_at_iso: str
    pr_number: int | None = None
    issue_number: int | None = None


# ── Graph reader protocol (duck-typed) ───────────────────────────────────


class _GraphReader(Protocol):
    """Minimal surface :class:`MemoryRetriever` needs from the graph store."""

    async def list_nodes_with_properties(
        self,
        label: str,
        *,
        where: str | None = None,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]: ...


# ── Embedding / ranking helpers ──────────────────────────────────────────


_SUMMARY_CHAR_CAP = 200
"""Hard cap on recap length — matches the ``CoreMemoryHit.summary`` convention."""

_TOKEN_CHAR_RATIO = 4
"""Conservative "4 chars = 1 token" rule for the budget check."""


def _cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity in ``[-1.0, 1.0]`` — 0.0 when either vector is empty.

    We clamp the result to ``[0.0, 1.0]`` before returning because the
    retriever surfaces ``similarity`` as a non-negative confidence; a
    negative cosine on short text almost always means "unrelated" and
    we don't want it rendered as a suspiciously-negative number in the
    prompt.
    """
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    raw = dot / (na * nb)
    return max(0.0, min(1.0, raw))


def _jaccard(a: str, b: str) -> float:
    """Jaccard word-overlap similarity on lowercase token sets.

    Cheap, deterministic, and decent enough for short titles / labels.
    Empty strings and fully-disjoint inputs both return 0.0.
    """
    tokens_a = {t for t in a.lower().split() if t}
    tokens_b = {t for t in b.lower().split() if t}
    if not tokens_a or not tokens_b:
        return 0.0
    intersection = tokens_a & tokens_b
    union = tokens_a | tokens_b
    if not union:
        return 0.0
    return len(intersection) / len(union)


def _parse_stored_embedding(raw: Any) -> list[float]:
    """Decode the ``summary_embedding`` property back into ``list[float]``.

    The write path stores embeddings as comma-separated strings because
    the graph writer's retry loop is simpler when every property is a
    scalar. Parsing tolerates extra whitespace, empty strings, and
    malformed entries — a single bad float drops the vector entirely so
    the retriever falls through to Jaccard instead of ranking with a
    half-broken vector.
    """
    if raw is None:
        return []
    if isinstance(raw, list):
        try:
            return [float(v) for v in raw]
        except (TypeError, ValueError):
            return []
    if not isinstance(raw, str):
        return []
    stripped = raw.strip()
    if not stripped:
        return []
    try:
        return [float(part) for part in stripped.split(",") if part.strip()]
    except ValueError:
        return []


# ── Retriever ────────────────────────────────────────────────────────────


class MemoryRetriever:
    """Rank prior :class:`AgentCoreMemory` nodes by similarity to a query.

    Args:
        graph: Anything exposing
            :meth:`caretaker.graph.store.GraphStore.list_nodes_with_properties`.
            Tests pass a small fake; production wires the real store.
        embedder: Optional :class:`Embedder`. When supplied and
            ``available``, ranking uses cosine similarity against stored
            ``summary_embedding`` vectors. When ``None`` or unavailable
            (or no vectors are stored) the retriever falls back to
            Jaccard word-overlap on ``summary``.
        max_hits: Hard cap on returned hits (default 3).
        max_tokens: Budget cap used by :meth:`format_for_prompt` — the
            rendered markdown block will not exceed roughly this many
            tokens (estimated at 4 chars/token for safety).
    """

    def __init__(
        self,
        *,
        graph: _GraphReader,
        embedder: Embedder | None = None,
        max_hits: int = 3,
        max_tokens: int = 500,
    ) -> None:
        self._graph = graph
        self._embedder = embedder
        self._max_hits = max(0, max_hits)
        self._max_tokens = max(0, max_tokens)

    async def find_relevant(
        self,
        agent: str,
        query_text: str,
        *,
        repo_slug: str | None = None,
    ) -> list[CoreMemoryHit]:
        """Return up to ``max_hits`` prior memory snapshots, ranked by similarity.

        ``query_text`` is embedded (when an embedder is available) and
        compared against the ``summary_embedding`` property on each
        stored node. Rows that lack a stored embedding fall back to
        Jaccard overlap on ``summary`` vs ``query_text``. The two
        similarity spaces are not perfectly comparable — Jaccard is
        bounded in ``[0, 1]`` by construction and cosine is clamped to
        the same range — so mixed-mode rankings stay stable enough for
        top-k selection.

        The ranking is deterministic: ties are broken by ``run_at``
        descending (most recent first) so the prompt always surfaces
        the freshest context when two rows tie.
        """
        if self._max_hits == 0:
            return []
        # Scope filter — always by agent, optionally by repo_slug.
        where_clauses = ["n.agent = $agent"]
        params: dict[str, Any] = {"agent": agent}
        if repo_slug:
            where_clauses.append("n.repo = $repo")
            params["repo"] = repo_slug
        where = " AND ".join(where_clauses)

        try:
            rows = await self._graph.list_nodes_with_properties(
                "AgentCoreMemory",
                where=where,
                params=params,
            )
        except Exception as exc:  # noqa: BLE001 - retrieval must never fail the dispatch
            logger.info(
                "MemoryRetriever.find_relevant: graph read failed for agent=%s: %s",
                agent,
                exc,
            )
            return []

        if not rows:
            return []

        # Embed the query once. When the embedder is absent or returns an
        # empty vector every row drops to Jaccard, which is intentional.
        query_vector: list[float] = []
        if self._embedder is not None and getattr(self._embedder, "available", False):
            try:
                query_vector = await self._embedder.embed(query_text)
            except Exception as exc:  # noqa: BLE001 - fall back to Jaccard on any embed error
                logger.info("MemoryRetriever: query embed failed: %s", exc)
                query_vector = []

        scored: list[tuple[float, str, CoreMemoryHit]] = []
        for row in rows:
            summary = str(row.get("summary") or "").strip()
            if not summary:
                # Nothing to compare against; skip silently so older rows
                # written before summaries were persisted don't appear in
                # the prompt as blank bullets.
                continue
            stored_vec = _parse_stored_embedding(row.get("summary_embedding"))
            if query_vector and stored_vec:
                sim = _cosine(query_vector, stored_vec)
            else:
                sim = _jaccard(query_text, summary)

            run_at = str(row.get("run_at") or row.get("observed_at") or "")
            pr_number = _coerce_int(row.get("pr_number"))
            issue_number = _coerce_int(row.get("issue_number"))
            # Fallback to ``active_pr`` for legacy rows written before T-E2
            # started persisting ``pr_number`` separately.
            if pr_number is None:
                pr_number = _coerce_int(row.get("active_pr"))

            hit = CoreMemoryHit(
                agent=str(row.get("agent") or agent),
                summary=summary[:_SUMMARY_CHAR_CAP],
                outcome=str(row.get("outcome") or "unknown"),
                similarity=round(sim, 4),
                run_at_iso=run_at,
                pr_number=pr_number,
                issue_number=issue_number,
            )
            scored.append((sim, run_at, hit))

        if not scored:
            return []

        # Sort: primary = similarity desc, tiebreak = run_at desc (most
        # recent wins). Empty run_at strings sort last.
        scored.sort(key=lambda t: (-t[0], -_run_at_sort_key(t[1])))
        return [hit for _, _, hit in scored[: self._max_hits]]

    def format_for_prompt(self, hits: list[CoreMemoryHit]) -> str:
        """Render ``hits`` into a stable markdown block for prompt injection.

        The output shape is a section titled ``## Relevant past runs``
        followed by one bullet per hit. Summaries are truncated so the
        entire block stays under ``max_tokens`` (estimated at 4 chars
        per token). Later hits are truncated first — the top hit always
        surfaces, even if that means dropping trailing hits entirely.

        Returns an empty string when ``hits`` is empty. Callers should
        treat an empty return as "no memory context available" and skip
        the injection rather than emit a blank heading.
        """
        if not hits:
            return ""

        char_budget = self._max_tokens * _TOKEN_CHAR_RATIO
        header = "## Relevant past runs"
        intro = (
            "Prior agent dispatches that resemble this one. Use for context; "
            "these are memories, not rules."
        )

        # Always include the header so the LLM knows what it's looking at.
        # The intro is a nice-to-have; drop it when the budget is tight.
        fixed = f"{header}\n{intro}\n"
        if len(fixed) > char_budget // 2:
            fixed = f"{header}\n"
        remaining = max(0, char_budget - len(fixed))

        lines: list[str] = []
        for idx, hit in enumerate(hits):
            bullet = _render_hit_bullet(hit)
            budget_here = remaining
            if idx == 0:
                # Top hit always makes it — truncate its summary to fit the
                # remaining budget. Accept the bullet even if truncation
                # yields a minimal metadata-only rendering.
                if len(bullet) + 1 > budget_here:
                    bullet = _truncate_hit_bullet(hit, max_chars=max(16, budget_here - 1))
                lines.append(bullet)
                remaining -= len(bullet) + 1
                continue
            if len(bullet) + 1 > budget_here:
                break
            lines.append(bullet)
            remaining -= len(bullet) + 1

        if not lines:
            return ""

        return fixed + "\n".join(lines) + "\n"


# ── Internal helpers ─────────────────────────────────────────────────────


def _coerce_int(raw: Any) -> int | None:
    """Best-effort int coercion for numeric node properties.

    Neo4j may surface numbers as ``int`` already, but the writer
    accepts ``None`` and strings too. Anything that doesn't round-trip
    cleanly returns ``None`` so the pydantic model stays well-typed.
    """
    if raw is None or raw == "":
        return None
    if isinstance(raw, bool):
        # ``bool`` is a subtype of ``int`` — don't surface True/False
        # as 1/0 in a PR-number field.
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _run_at_sort_key(run_at: str) -> int:
    """Stable numeric key for ISO timestamps so newer rows win ties."""
    if not run_at:
        return 0
    # Strip separators and keep the first 14 digits (YYYYMMDDHHMMSS-ish).
    digits = "".join(ch for ch in run_at if ch.isdigit())
    if not digits:
        return 0
    return int(digits[:14])


def _render_hit_bullet(hit: CoreMemoryHit) -> str:
    """Render a single hit as one markdown bullet."""
    ref = ""
    if hit.pr_number is not None:
        ref = f" (PR #{hit.pr_number})"
    elif hit.issue_number is not None:
        ref = f" (issue #{hit.issue_number})"
    return f"- **{hit.outcome}** · sim={hit.similarity:.2f}{ref}: {hit.summary}"


def _truncate_hit_bullet(hit: CoreMemoryHit, *, max_chars: int) -> str:
    """Truncate the bullet for the top hit so it fits in the budget.

    We trim the summary first (keeping the prefix/outcome/ref metadata)
    because those fields carry the decision-relevant signal.
    """
    bullet = _render_hit_bullet(hit)
    if len(bullet) <= max_chars:
        return bullet
    # Figure out how much space is left for the summary after the
    # fixed prefix. Worst case: leave a 1-char summary ending in "...".
    fixed = bullet[: bullet.rfind(hit.summary)] if hit.summary in bullet else ""
    available = max(4, max_chars - len(fixed))
    truncated_summary = hit.summary[: max(1, available - 3)] + "..."
    return fixed + truncated_summary


__all__ = ["CoreMemoryHit", "MemoryRetriever"]
