"""Tests for T-E3 — skill promotion round-trip.

Covers the read-loop close-out on ``promote_global_skills``:

* :meth:`InsightStore.get_relevant` returns the union of local
  ``:Skill`` hits and ``:GlobalSkill`` hits exposed via the injected
  :class:`GlobalSkillReader`.
* Deduplication keys on ``signature`` — local wins over global.
* ``fleet.include_global_in_prompts = False`` (i.e.
  ``include_global=False`` on the store) returns only local hits.
* The foundry prompt renderer prefixes global hits with ``[fleet]``.
* Cross-repo end-to-end: skill promoted from two repo slugs via
  :func:`promote_global_skills` is surfaced to a third repo's
  ``get_relevant`` call through :class:`GraphBackedGlobalSkillReader`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from caretaker.evolution.insight_store import (
    CATEGORY_CI,
    GlobalSkillReader,
    InsightStore,
    Skill,
)
from caretaker.fleet.graph import (
    GraphBackedGlobalSkillReader,
    promote_global_skills,
)
from caretaker.foundry.prompts import _format_skills
from caretaker.graph.models import NodeType

# ── Fakes ────────────────────────────────────────────────────────────────


class _StaticGlobalReader:
    """Pre-baked :GlobalSkill rows for isolated ``get_relevant`` tests."""

    def __init__(self, skills: list[Skill]) -> None:
        self._skills = skills
        self.calls: list[str] = []

    def list_global_skills(self, category: str) -> list[Skill]:
        self.calls.append(category)
        return [s for s in self._skills if not s.category or s.category == category]


def _global_skill(signature: str, sop: str, *, repo_count: int = 3) -> Skill:
    """Shape a :class:`Skill` the way a graph-backed reader would."""
    return Skill(
        id=f"global_skill:{signature}",
        category=CATEGORY_CI,
        signature=signature,
        sop_text=sop,
        success_count=repo_count,
        fail_count=0,
        last_used_at=None,
        created_at=datetime.now(UTC),
        scope="global",
    )


class _FakeGraphStore:
    """Minimal async :class:`GraphStore` supporting the T-E3 round-trip.

    Stores nodes and edges in memory and serves ``list_skill_rows`` +
    ``list_global_skill_rows`` from the same dict so writes made by
    :func:`promote_global_skills` are visible to a subsequent read via
    :class:`GraphBackedGlobalSkillReader`.
    """

    def __init__(self, skill_rows: list[dict[str, Any]] | None = None) -> None:
        self._skill_rows = list(skill_rows or [])
        self._global_skills: dict[str, dict[str, Any]] = {}
        self.edges: list[tuple[str, str, str, str, str, dict[str, Any]]] = []

    async def merge_node(self, label: str, node_id: str, properties: dict[str, Any]) -> None:
        if label == NodeType.GLOBAL_SKILL:
            self._global_skills[node_id] = {"id": node_id, **properties}

    async def merge_edge(
        self,
        source_label: str,
        source_id: str,
        target_label: str,
        target_id: str,
        rel_type: str,
        properties: dict[str, Any] | None = None,
    ) -> None:
        self.edges.append(
            (source_label, source_id, target_label, target_id, rel_type, properties or {})
        )

    async def list_skill_rows(self) -> list[dict[str, Any]]:
        return list(self._skill_rows)

    async def list_global_skill_rows(self, category: str | None = None) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for node in self._global_skills.values():
            node_category = node.get("category") or ""
            if category is not None and node_category and node_category != category:
                continue
            rows.append(
                {
                    "id": node["id"],
                    "signature": node.get("signature", ""),
                    "name": node.get("name", ""),
                    "category": node_category,
                    "sop_text": node.get("abstracted_sop_text", ""),
                    "repo_count": node.get("repo_count", 0),
                    "abstracted_at": node.get("abstracted_at", ""),
                }
            )
        return rows


class _FakeInsightStoreForPromotion:
    """Duck-type matching :class:`InsightStore` for :func:`promote_global_skills`."""

    def __init__(self, sops: dict[str, str]) -> None:
        self._sops = sops

    def all_skills(self) -> list[Any]:
        class _S:
            def __init__(self, signature: str, sop_text: str) -> None:
                self.signature = signature
                self.sop_text = sop_text

        return [_S(sig, sop) for sig, sop in self._sops.items()]


def _skill_row(*, signature: str, repo: str, category: str = CATEGORY_CI) -> dict[str, Any]:
    return {
        "id": f"skill:{category}:{signature}:{repo}",
        "signature": signature,
        "repo": repo,
        "name": signature,
        "category": category,
    }


# ── get_relevant: union + dedupe + scope ─────────────────────────────────


class TestGetRelevantUnion:
    def test_union_returns_local_and_global_with_scope(self) -> None:
        store = InsightStore(
            db_path=":memory:",
            global_skill_reader=_StaticGlobalReader(
                [_global_skill("fleet_only", "Apply fleet pattern.")]
            ),
            include_global=True,
        )
        # Build a local hit with confidence >= 0.5.
        for _ in range(4):
            store.record_success(CATEGORY_CI, "local_only", "Patch local issue.")

        results = store.get_relevant(CATEGORY_CI, "anything")

        signatures = {s.signature for s in results}
        assert signatures == {"local_only", "fleet_only"}

        scopes = {s.signature: s.scope for s in results}
        assert scopes["local_only"] == "local"
        assert scopes["fleet_only"] == "global"

    def test_dedupe_prefers_local_over_global(self) -> None:
        """Same signature in both tiers → local wins; global copy dropped."""
        store = InsightStore(
            db_path=":memory:",
            global_skill_reader=_StaticGlobalReader(
                [_global_skill("shared_sig", "Fleet-abstracted SOP.")]
            ),
            include_global=True,
        )
        for _ in range(4):
            store.record_success(CATEGORY_CI, "shared_sig", "Repo-local verified SOP.")

        results = store.get_relevant(CATEGORY_CI, "anything")

        assert len(results) == 1
        assert results[0].signature == "shared_sig"
        assert results[0].scope == "local"
        assert "Repo-local" in results[0].sop_text

    def test_include_global_false_returns_only_local(self) -> None:
        reader = _StaticGlobalReader([_global_skill("fleet_only", "Fleet SOP.")])
        store = InsightStore(
            db_path=":memory:",
            global_skill_reader=reader,
            include_global=False,
        )
        for _ in range(4):
            store.record_success(CATEGORY_CI, "local_only", "Local SOP.")

        results = store.get_relevant(CATEGORY_CI, "anything")

        assert {s.signature for s in results} == {"local_only"}
        # Reader isn't even consulted — a misfiring fleet skill must not
        # cost a graph query when the operator has flipped this off.
        assert reader.calls == []

    def test_no_reader_is_local_only(self) -> None:
        store = InsightStore(db_path=":memory:")  # no reader wired
        for _ in range(4):
            store.record_success(CATEGORY_CI, "local_only", "Local SOP.")

        results = store.get_relevant(CATEGORY_CI, "anything")

        assert [s.signature for s in results] == ["local_only"]
        assert results[0].scope == "local"

    def test_reader_hit_missing_scope_is_normalised(self) -> None:
        """Defensive: readers that return scope="local" get re-tagged."""
        leaky_hit = Skill(
            id="global_skill:leaky",
            category=CATEGORY_CI,
            signature="leaky",
            sop_text="Misbehaving reader SOP.",
            success_count=3,
            fail_count=0,
            last_used_at=None,
            created_at=datetime.now(UTC),
            scope="local",  # intentionally wrong
        )

        class _LeakyReader:
            def list_global_skills(self, category: str) -> list[Skill]:
                return [leaky_hit]

        reader: GlobalSkillReader = _LeakyReader()
        store = InsightStore(db_path=":memory:", global_skill_reader=reader)

        results = store.get_relevant(CATEGORY_CI, "anything")

        assert len(results) == 1
        assert results[0].signature == "leaky"
        assert results[0].scope == "global"


# ── Prompt renderer surfaces the scope ───────────────────────────────────


class TestFormatSkillsScope:
    def test_fleet_prefix_on_global_hit(self) -> None:
        local = Skill(
            id="ci:local",
            category=CATEGORY_CI,
            signature="local_sig",
            sop_text="Restart the test runner.",
            success_count=4,
            fail_count=0,
            last_used_at=None,
            created_at=datetime.now(UTC),
            scope="local",
        )
        fleet = _global_skill("fleet_sig", "Pin the flaky dep.")

        rendered = _format_skills([local, fleet])

        assert "Hints from past successful fixes" in rendered
        assert "- Restart the test runner." in rendered
        assert "- [fleet] Pin the flaky dep." in rendered
        # Local hits must NOT carry the [fleet] prefix.
        local_line = next(
            line for line in rendered.splitlines() if "Restart the test runner" in line
        )
        assert "[fleet]" not in local_line

    def test_empty_skills_block_is_empty_string(self) -> None:
        assert _format_skills(None) == ""
        assert _format_skills([]) == ""


# ── Cross-repo end-to-end round-trip ─────────────────────────────────────


@pytest.mark.asyncio
async def test_cross_repo_promotion_surfaces_via_get_relevant() -> None:
    """Skill promoted in two repos → visible to a third repo's get_relevant.

    1. Seed the graph with ``:Skill`` rows from *two* distinct repo
       slugs carrying the same signature.
    2. Run :func:`promote_global_skills` with ``min_repos=2`` and
       ``share_skills=True`` — a ``:GlobalSkill`` node lands on the graph.
    3. Build a third-repo :class:`InsightStore` (fresh SQLite, no local
       ``:Skill`` for the signature) wired to a
       :class:`GraphBackedGlobalSkillReader` pointing at the same graph.
    4. ``get_relevant`` on the third repo surfaces the promoted signature
       with ``scope="global"``.
    """
    rows = [
        _skill_row(signature="retry_flaky_test", repo="acme/widgets"),
        _skill_row(signature="retry_flaky_test", repo="acme/gadgets"),
    ]
    graph = _FakeGraphStore(skill_rows=rows)
    insight_for_promotion = _FakeInsightStoreForPromotion(
        {"retry_flaky_test": "Retry the failing job once, then escalate."}
    )

    promoted = await promote_global_skills(
        graph,
        insight_for_promotion,
        min_repos=2,
        share_skills=True,
    )
    assert promoted == ["retry_flaky_test"]

    # Third-repo store: empty local backend, graph-backed reader.
    reader = GraphBackedGlobalSkillReader(graph)
    third_repo_store = InsightStore(
        db_path=":memory:",
        global_skill_reader=reader,
        include_global=True,
    )

    # No local hit for the signature — purely fleet surfacing.
    hits = third_repo_store.get_relevant(CATEGORY_CI, "retry_flaky_test")

    assert len(hits) == 1
    assert hits[0].signature == "retry_flaky_test"
    assert hits[0].scope == "global"
    # The abstracted SOP text survived the promotion pipeline.
    assert "Retry" in hits[0].sop_text
