"""Tests for M4 of the memory-graph plan — tiered compaction + salience.

Mirrors the ``RecordingStore`` pattern from ``tests/test_graph_builder_m3.py``
but extends it with a ``deleted`` list so :func:`prune_low_salience` can be
exercised without a live Neo4j. The scoring tests stay pure-stdlib and
exercise :func:`compute_salience` on synthetic :class:`RunSummary`
instances.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest

from caretaker.graph import compaction
from caretaker.graph.models import NodeType
from caretaker.state.models import RunSummary


class RecordingCompactionStore:
    """Fake :class:`GraphStore` exposing the compaction-protocol surface.

    Extends the M3 ``RecordingStore`` pattern with ``deleted`` + a
    pre-seeded ``node_rows`` map so tests can stage tier-0 rows, run
    the compaction routines, then assert on what was merged / deleted.
    """

    def __init__(self) -> None:
        self.nodes: list[tuple[str, str, dict[str, Any]]] = []
        self.deleted: list[tuple[str, str]] = []
        # label → list of property dicts returned by ``list_nodes_*``.
        # Tests populate this directly; the compaction code never
        # mutates it.
        self.node_rows: dict[str, list[dict[str, Any]]] = {}

    async def ensure_indexes(self) -> None:
        # Unused by compaction but kept for symmetry with the real
        # store — avoids surprise attribute errors if a future caller
        # routes through here.
        return None

    async def merge_node(self, label: str, node_id: str, props: dict[str, Any]) -> None:
        self.nodes.append((label, node_id, props))

    async def list_nodes_with_properties(
        self,
        label: str,
        *,
        where: str | None = None,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Return seeded rows, filtered by the ``repo`` param + run_at window.

        The real store pushes filtering into cypher, but the fake
        reproduces the subset compaction actually uses: tenant scoping
        via ``n.repo = $repo`` and run_at bounds for the rollup query.
        """
        rows = list(self.node_rows.get(label, []))
        if params is None:
            return rows
        repo = params.get("repo")
        if repo is not None:
            rows = [r for r in rows if r.get("repo") == repo]
        start = params.get("start")
        end = params.get("end")
        if start and end:
            rows = [
                r
                for r in rows
                if isinstance(r.get("run_at"), str) and r["run_at"] and start <= r["run_at"] < end
            ]
        return rows

    async def delete_node(self, label: str, node_id: str) -> None:
        self.deleted.append((label, node_id))


# ── Salience scoring ────────────────────────────────────────────────────────


def test_compute_salience_is_bounded_zero_to_one() -> None:
    """The weighted sum must stay in ``[0, 1]`` for any plausible input."""
    # Freshly-emitted run with no escalations, no errors, high connectivity:
    # the weights sum to 1 so the bound is contract-level.
    run = RunSummary(
        run_at=datetime.now(UTC),
        prs_escalated=100,
        issues_escalated=100,
        goal_escalation_count=50,
        errors=["failure-a", "failure-b"],
        escalation_rate=1.0,
    )
    value = compaction.compute_salience(run, connectivity=9999)
    assert 0.0 <= value <= 1.0

    # Ancient run, no signal at all — still bounded.
    ancient = RunSummary(
        run_at=datetime(2020, 1, 1, tzinfo=UTC),
        prs_escalated=0,
        errors=[],
        escalation_rate=0.0,
    )
    value = compaction.compute_salience(ancient, connectivity=0)
    assert 0.0 <= value <= 1.0


def test_high_escalation_run_outranks_low_escalation_run() -> None:
    """The escalation signal must dominate: same-age runs rank by escalations."""
    now = datetime(2026, 4, 21, tzinfo=UTC)
    run_at = now - timedelta(days=1)

    low = RunSummary(
        run_at=run_at,
        prs_escalated=0,
        issues_escalated=0,
        errors=[],
        escalation_rate=0.0,
    )
    high = RunSummary(
        run_at=run_at,
        prs_escalated=5,
        issues_escalated=3,
        goal_escalation_count=2,
        errors=["ci failure"],
        escalation_rate=0.8,
    )
    low_score = compaction.compute_salience(low, connectivity=0, now=now)
    high_score = compaction.compute_salience(high, connectivity=0, now=now)
    assert high_score > low_score


def test_compute_salience_handles_missing_fields_gracefully() -> None:
    """A bare dict with only ``run_at`` must not raise."""
    now = datetime(2026, 4, 21, tzinfo=UTC)
    value = compaction.compute_salience(
        {"run_at": (now - timedelta(days=2)).isoformat()},
        connectivity=None,
        now=now,
    )
    assert 0.0 <= value <= 1.0


def test_compute_salience_recency_decays_to_zero_for_very_old_runs() -> None:
    """Runs from years ago contribute almost nothing to the recency term."""
    now = datetime(2026, 4, 21, tzinfo=UTC)
    ancient = RunSummary(run_at=datetime(2020, 1, 1, tzinfo=UTC))
    # No escalations, no errors, default connectivity ⇒ only the recency
    # term is non-zero, and it should be tiny after ≥ 6 years.
    score = compaction.compute_salience(ancient, connectivity=0, now=now)
    assert score < 0.05


# ── Tier-0 → Tier-1 rollup ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_compact_tier0_to_tier1_aggregates_weekly_counters() -> None:
    """Weekly rollup sums PRs + issues and means escalation_rate."""
    store = RecordingCompactionStore()
    # 2026-W16 is the week of Mon 2026-04-13. Seed three runs inside it
    # for repo ``acme/widgets`` plus one for an unrelated repo that
    # must be ignored.
    week_start = datetime(2026, 4, 13, tzinfo=UTC)
    store.node_rows[NodeType.RUN.value] = [
        {
            "id": f"run:{(week_start + timedelta(hours=h)).isoformat()}",
            "repo": "acme/widgets",
            "run_at": (week_start + timedelta(hours=h)).isoformat(),
            "prs_merged": pm,
            "issues_triaged": it,
            "escalation_rate": er,
        }
        for h, pm, it, er in [(2, 3, 5, 0.1), (10, 0, 2, 0.2), (40, 2, 1, 0.3)]
    ]
    store.node_rows[NodeType.RUN.value].append(
        {
            "id": "run:2026-04-15T00:00:00+00:00",
            "repo": "other/repo",
            "run_at": "2026-04-15T00:00:00+00:00",
            "prs_merged": 99,
            "issues_triaged": 99,
            "escalation_rate": 0.99,
        }
    )

    node = await compaction.compact_tier0_to_tier1(store, "acme/widgets", date(2026, 4, 15))
    assert node.type == NodeType.RUN_SUMMARY_WEEK.value
    assert node.properties["repo"] == "acme/widgets"
    assert node.properties["week_of"] == "2026-04-13"
    assert node.properties["week_key"] == "2026-W16"
    assert node.properties["run_count"] == 3
    assert node.properties["prs_merged_total"] == 5  # 3 + 0 + 2
    assert node.properties["issues_triaged_total"] == 8  # 5 + 2 + 1
    assert node.properties["escalation_rate_mean"] == pytest.approx(0.2)
    assert node.properties["top_skill_ids"] == []
    assert "rolled_up_at" in node.properties

    # Exactly one merged node — the rollup, under the expected label.
    assert len(store.nodes) == 1
    label, node_id, props = store.nodes[0]
    assert label == NodeType.RUN_SUMMARY_WEEK.value
    assert node_id == "runweek:acme/widgets:2026-W16"
    assert props["run_count"] == 3


@pytest.mark.asyncio
async def test_compact_tier0_to_tier1_empty_week_produces_zero_counters() -> None:
    """No matching runs → a rollup node with ``run_count=0`` and mean=0.0."""
    store = RecordingCompactionStore()
    store.node_rows[NodeType.RUN.value] = []
    node = await compaction.compact_tier0_to_tier1(store, "acme/widgets", date(2026, 4, 15))
    assert node.properties["run_count"] == 0
    assert node.properties["prs_merged_total"] == 0
    assert node.properties["escalation_rate_mean"] == 0.0


# ── Pruning ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_prune_low_salience_deletes_aged_low_score_nodes() -> None:
    """Default 30-day / 0.25 gate evicts stale, low-signal runs."""
    store = RecordingCompactionStore()
    now = datetime(2026, 4, 21, tzinfo=UTC)
    store.node_rows[NodeType.RUN.value] = [
        {
            # Ancient + zero signal — should be pruned.
            "id": "run:ancient",
            "repo": "acme/widgets",
            "run_at": (now - timedelta(days=120)).isoformat(),
            "prs_escalated": 0,
            "errors": [],
            "escalation_rate": 0.0,
        },
        {
            # Recent — age gate alone spares it regardless of salience.
            "id": "run:yesterday",
            "repo": "acme/widgets",
            "run_at": (now - timedelta(days=1)).isoformat(),
            "prs_escalated": 0,
            "errors": [],
            "escalation_rate": 0.0,
        },
        {
            # Ancient but high escalation count → high salience spares it.
            "id": "run:ancient-hot",
            "repo": "acme/widgets",
            "run_at": (now - timedelta(days=120)).isoformat(),
            "prs_escalated": 9,
            "issues_escalated": 9,
            "errors": ["hard-failure"],
            "escalation_rate": 0.9,
        },
    ]

    pruned = await compaction.prune_low_salience(store, "acme/widgets", now=now)
    assert pruned == 1
    assert store.deleted == [(NodeType.RUN.value, "run:ancient")]


@pytest.mark.asyncio
async def test_prune_low_salience_respects_pinned_flag() -> None:
    """A pinned node is never deleted, even when stale and low-score."""
    store = RecordingCompactionStore()
    now = datetime(2026, 4, 21, tzinfo=UTC)
    store.node_rows[NodeType.RUN.value] = [
        {
            "id": "run:pinned-bool",
            "repo": "acme/widgets",
            "run_at": (now - timedelta(days=400)).isoformat(),
            "pinned": True,
            "errors": [],
            "escalation_rate": 0.0,
        },
        {
            "id": "run:pinned-str",
            "repo": "acme/widgets",
            "run_at": (now - timedelta(days=400)).isoformat(),
            # Some older writers persist ``pinned`` as a string — the
            # prune pass must normalise both representations.
            "pinned": "true",
            "errors": [],
            "escalation_rate": 0.0,
        },
    ]

    pruned = await compaction.prune_low_salience(store, "acme/widgets", now=now)
    assert pruned == 0
    assert store.deleted == []


@pytest.mark.asyncio
async def test_prune_low_salience_ignores_other_repos() -> None:
    """The ``repo`` filter keeps tenant boundaries intact during pruning."""
    store = RecordingCompactionStore()
    now = datetime(2026, 4, 21, tzinfo=UTC)
    store.node_rows[NodeType.RUN.value] = [
        {
            "id": "run:other-tenant-ancient",
            "repo": "other/repo",
            "run_at": (now - timedelta(days=400)).isoformat(),
            "errors": [],
            "escalation_rate": 0.0,
        },
    ]
    pruned = await compaction.prune_low_salience(store, "acme/widgets", now=now)
    assert pruned == 0
    assert store.deleted == []


@pytest.mark.asyncio
async def test_prune_low_salience_skips_goal_and_skill_labels() -> None:
    """``:Goal`` / ``:Skill`` are never candidates — safety over completeness."""
    store = RecordingCompactionStore()
    now = datetime(2026, 4, 21, tzinfo=UTC)
    # Seed a Goal that would look stale + low-signal if it were a Run.
    store.node_rows[NodeType.GOAL.value] = [
        {
            "id": "goal:ci_health",
            "repo": "acme/widgets",
            "run_at": (now - timedelta(days=9999)).isoformat(),
            "errors": [],
            "escalation_rate": 0.0,
        },
    ]
    store.node_rows[NodeType.SKILL.value] = [
        {
            "id": "skill:ci/unknown",
            "repo": "acme/widgets",
            "run_at": (now - timedelta(days=9999)).isoformat(),
            "errors": [],
            "escalation_rate": 0.0,
        },
    ]
    # No runs present at all — prune should still return 0 and never
    # have touched the ``Goal`` / ``Skill`` labels (the labels aren't
    # in ``_PRUNABLE_LABELS``, so the fake store's rows for them
    # should remain untouched).
    pruned = await compaction.prune_low_salience(store, "acme/widgets", now=now)
    assert pruned == 0
    assert store.deleted == []
    # And the Goal / Skill labels must not have been queried for
    # deletion — cross-check by label presence in the deleted log.
    assert not any(d[0] in {NodeType.GOAL.value, NodeType.SKILL.value} for d in store.deleted)


# ── Nightly orchestrator ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_nightly_rolls_up_last_week_and_prunes() -> None:
    """The one-shot entry point returns a counter dict with both stages."""
    store = RecordingCompactionStore()
    now = datetime(2026, 4, 21, tzinfo=UTC)

    # Seed one run in last week (will roll up) and one ancient run
    # (will prune).
    last_week_ts = (now - timedelta(days=7)).isoformat()
    store.node_rows[NodeType.RUN.value] = [
        {
            "id": f"run:{last_week_ts}",
            "repo": "acme/widgets",
            "run_at": last_week_ts,
            "prs_merged": 2,
            "issues_triaged": 1,
            "escalation_rate": 0.1,
            "errors": [],
        },
        {
            "id": "run:ancient",
            "repo": "acme/widgets",
            "run_at": (now - timedelta(days=120)).isoformat(),
            "prs_merged": 0,
            "issues_triaged": 0,
            "escalation_rate": 0.0,
            "errors": [],
        },
    ]

    counts = await compaction.run_nightly(store, "acme/widgets", now=now)
    assert counts["rolled_up_runs"] == 1
    assert counts["pruned"] == 1
    # The rolled-up week node landed.
    rollup_nodes = [n for n in store.nodes if n[0] == NodeType.RUN_SUMMARY_WEEK.value]
    assert len(rollup_nodes) == 1
    assert store.deleted == [(NodeType.RUN.value, "run:ancient")]
