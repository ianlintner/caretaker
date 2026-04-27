"""Tests for M6 of the memory-graph plan — fleet graph + GlobalSkill promotion.

Covers:

* :func:`sync_repos_to_graph` emits ``:Repo``/``RUNS_AGENT``/``GOAL_HEALTH``
  entries for every known fleet client (multi-tenant fixture).
* :func:`abstract_sop` strips the four identifier classes and is idempotent.
* :func:`promote_global_skills` respects ``min_repos``, is gated behind
  ``share_skills``, and always runs the SOP through the abstraction pass
  before anything lands as a ``:GlobalSkill`` node.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from caretaker.fleet.abstraction import abstract_sop
from caretaker.fleet.graph import promote_global_skills, sync_repos_to_graph
from caretaker.fleet.store import FleetRegistryStore
from caretaker.graph.models import NodeType, RelType


class RecordingStore:
    """Fake :class:`GraphStore` — records merge calls + skill rows.

    Mirrors the shape used in the M2 / M3 graph-builder tests so
    assertions stay comparable.
    """

    def __init__(self, skill_rows: list[dict[str, Any]] | None = None) -> None:
        self.nodes: list[tuple[str, str, dict[str, Any]]] = []
        self.edges: list[tuple[str, str, str, str, str, dict[str, Any]]] = []
        self._skill_rows = list(skill_rows or [])

    async def merge_node(self, label: str, node_id: str, props: dict[str, Any]) -> None:
        self.nodes.append((label, node_id, props))

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


_EdgeTuple = tuple[str, str, str, str, str, dict[str, Any]]


def _nodes_of(store: RecordingStore, label: str) -> list[tuple[str, str, dict[str, Any]]]:
    return [n for n in store.nodes if n[0] == label]


def _edges_of(store: RecordingStore, rel: str) -> list[_EdgeTuple]:
    return [e for e in store.edges if e[4] == rel]


# ── sync_repos_to_graph ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sync_repos_emits_repo_runs_agent_and_goal_health() -> None:
    """Two-client fleet → two Repos, RUNS_AGENT per enabled agent, GOAL_HEALTH."""
    fleet = FleetRegistryStore()
    await fleet.record_heartbeat(
        {
            "repo": "acme/widgets",
            "caretaker_version": "0.11.0",
            "run_at": datetime(2026, 4, 20, 12, 0, tzinfo=UTC).isoformat(),
            "enabled_agents": ["pr", "issue"],
            "goal_health": 0.82,
        }
    )
    await fleet.record_heartbeat(
        {
            "repo": "acme/gadgets",
            "caretaker_version": "0.11.1",
            "run_at": datetime(2026, 4, 20, 12, 5, tzinfo=UTC).isoformat(),
            "enabled_agents": ["pr", "devops", "security"],
            "goal_health": 0.95,
        }
    )

    store = RecordingStore()
    counts = await sync_repos_to_graph(store, fleet)

    # Two :Repo nodes, one per client.
    repos = _nodes_of(store, NodeType.REPO.value)
    assert {n[1] for n in repos} == {"repo:acme/widgets", "repo:acme/gadgets"}
    for _, node_id, props in repos:
        slug = node_id.removeprefix("repo:")
        assert props["slug"] == slug
        assert props["repo"] == slug
        assert "last_heartbeat_at" in props
        assert props["caretaker_version"].startswith("0.11")

    # RUNS_AGENT edges fan out per enabled agent.
    runs_agent = _edges_of(store, RelType.RUNS_AGENT.value)
    assert len(runs_agent) == 5  # 2 + 3 agents across two repos
    edge_pairs = {(e[1], e[3]) for e in runs_agent}
    assert ("repo:acme/widgets", "agent:pr") in edge_pairs
    assert ("repo:acme/widgets", "agent:issue") in edge_pairs
    assert ("repo:acme/gadgets", "agent:security") in edge_pairs

    # GOAL_HEALTH edges carry score + as_of and target goal:overall.
    gh = _edges_of(store, RelType.GOAL_HEALTH.value)
    assert len(gh) == 2
    for src_label, _, tgt_label, tgt_id, _, props in gh:
        assert src_label == NodeType.REPO
        assert tgt_label == NodeType.GOAL
        assert tgt_id == "goal:overall"
        assert "score" in props
        assert "as_of" in props
        assert props["score"] in (0.82, 0.95)

    assert counts == {"repos": 2, "runs_agent_edges": 5, "goal_health_edges": 2}


@pytest.mark.asyncio
async def test_sync_repos_skips_goal_health_when_missing() -> None:
    """``last_goal_health is None`` → no GOAL_HEALTH edge, others still emitted."""
    fleet = FleetRegistryStore()
    await fleet.record_heartbeat(
        {
            "repo": "acme/widgets",
            "caretaker_version": "0.11.0",
            "enabled_agents": ["pr"],
            # No goal_health key.
        }
    )

    store = RecordingStore()
    counts = await sync_repos_to_graph(store, fleet)

    assert counts["repos"] == 1
    assert counts["runs_agent_edges"] == 1
    assert counts["goal_health_edges"] == 0
    assert _edges_of(store, RelType.GOAL_HEALTH.value) == []


@pytest.mark.asyncio
async def test_sync_repos_empty_fleet_returns_zero_counts() -> None:
    fleet = FleetRegistryStore()
    store = RecordingStore()
    counts = await sync_repos_to_graph(store, fleet)
    assert counts == {"repos": 0, "runs_agent_edges": 0, "goal_health_edges": 0}
    assert store.nodes == []
    assert store.edges == []


# ── abstract_sop ──────────────────────────────────────────────────────────


def test_abstract_sop_strips_all_four_identifier_classes() -> None:
    # Use distinct tokens so each identifier class is reached:
    # - "acme/widgets" is a plain repo slug (stripped by the slug regex,
    #   not the path regex, because ``acme-widgets`` — the deny_list
    #   token used for path scrubbing — doesn't match the slug itself).
    # - "src/acme-widgets/main.py" is a path embedding the deny_list token.
    text = (
        "When acme/widgets has a failure, ping @octocat to open #123. "
        "Touch src/acme-widgets/main.py and rerun."
    )
    out = abstract_sop(text, deny_list=["acme-widgets"])
    # Repo slug stripped.
    assert "acme/widgets" not in out
    # Handle stripped.
    assert "@octocat" not in out
    # Issue ref stripped.
    assert "#123" not in out
    # File path stripped.
    assert "src/acme-widgets/main.py" not in out
    # Placeholders present.
    assert "<repo>" in out
    assert "<user>" in out
    assert "<ref>" in out
    assert "<path>" in out


def test_abstract_sop_is_idempotent() -> None:
    """Running the redactor twice must produce identical output."""
    text = "In acme/widgets run @maintainer then close #42 at src/acme-widgets/run.py"
    once = abstract_sop(text, deny_list=["acme-widgets"])
    twice = abstract_sop(once, deny_list=["acme-widgets"])
    assert once == twice


def test_abstract_sop_handles_empty_input() -> None:
    assert abstract_sop("", deny_list=["acme"]) == ""
    assert abstract_sop("no identifiers at all", deny_list=None) == "no identifiers at all"


def test_abstract_sop_leaves_plain_prose_alone() -> None:
    """Sentences without any identifier class survive unchanged."""
    text = "Retry the CI step when it fails with a transient network error."
    assert abstract_sop(text, deny_list=["acme"]) == text


# ── promote_global_skills ─────────────────────────────────────────────────


def _skill_row(
    *, signature: str, repo: str, skill_id: str | None = None, category: str = "ci"
) -> dict[str, Any]:
    return {
        "id": skill_id or f"skill:{category}:{signature}:{repo}",
        "signature": signature,
        "repo": repo,
        "name": signature,
        "category": category,
    }


class _FakeInsightStore:
    """Minimal duck-type of :class:`InsightStore` used by promotion tests."""

    def __init__(self, sops: dict[str, str]) -> None:
        self._sops = sops

    def all_skills(self) -> list[Any]:
        # Return lightweight records that expose ``signature`` + ``sop_text``.
        class _S:
            def __init__(self, signature: str, sop_text: str) -> None:
                self.signature = signature
                self.sop_text = sop_text

        return [_S(sig, sop) for sig, sop in self._sops.items()]


@pytest.mark.asyncio
async def test_promote_requires_signature_in_min_repos() -> None:
    """Only signatures seen in ≥ min_repos distinct repos are promoted."""
    rows = [
        # "retry-flaky" appears in three repos → should promote with min_repos=3.
        _skill_row(signature="retry-flaky", repo="acme/widgets"),
        _skill_row(signature="retry-flaky", repo="acme/gadgets"),
        _skill_row(signature="retry-flaky", repo="other/thing"),
        # "single-repo" only in one → should NOT promote.
        _skill_row(signature="single-repo", repo="acme/widgets"),
        # "two-repos" in only two repos → also below threshold.
        _skill_row(signature="two-repos", repo="acme/widgets"),
        _skill_row(signature="two-repos", repo="acme/gadgets"),
    ]
    store = RecordingStore(skill_rows=rows)
    insight = _FakeInsightStore({"retry-flaky": "Retry the failing job once."})

    promoted = await promote_global_skills(store, insight, min_repos=3, share_skills=True)

    assert promoted == ["retry-flaky"]
    gs_nodes = _nodes_of(store, NodeType.GLOBAL_SKILL.value)
    assert len(gs_nodes) == 1
    _, node_id, props = gs_nodes[0]
    assert node_id == "global_skill:retry-flaky"
    assert props["signature"] == "retry-flaky"
    assert props["repo_count"] == 3
    assert "abstracted_sop_text" in props
    assert "abstracted_at" in props

    # PROMOTED_TO: one per source Skill node in the cluster.
    promoted_edges = _edges_of(store, RelType.PROMOTED_TO.value)
    assert len(promoted_edges) == 3
    assert all(e[0] == NodeType.SKILL and e[2] == NodeType.GLOBAL_SKILL for e in promoted_edges)

    # SHARES_SKILL: one per contributing repo.
    shares = _edges_of(store, RelType.SHARES_SKILL.value)
    assert len(shares) == 3
    share_sources = {e[1] for e in shares}
    assert share_sources == {
        "repo:acme/widgets",
        "repo:acme/gadgets",
        "repo:other/thing",
    }


@pytest.mark.asyncio
async def test_promote_is_a_noop_when_share_skills_disabled() -> None:
    """``share_skills=False`` blocks promotion even when min_repos is met."""
    rows = [
        _skill_row(signature="retry-flaky", repo="acme/widgets"),
        _skill_row(signature="retry-flaky", repo="acme/gadgets"),
        _skill_row(signature="retry-flaky", repo="other/thing"),
    ]
    store = RecordingStore(skill_rows=rows)
    insight = _FakeInsightStore({"retry-flaky": "Retry."})

    promoted = await promote_global_skills(store, insight, min_repos=3, share_skills=False)

    # Opt-in gate holds the line.
    assert promoted == []
    assert _nodes_of(store, NodeType.GLOBAL_SKILL.value) == []
    assert _edges_of(store, RelType.PROMOTED_TO.value) == []
    assert _edges_of(store, RelType.SHARES_SKILL.value) == []


@pytest.mark.asyncio
async def test_promote_always_runs_abstraction_on_sop_text() -> None:
    """SOP text landing on :GlobalSkill must be redacted, not raw."""
    rows = [
        _skill_row(signature="ping-owner", repo="acme/widgets"),
        _skill_row(signature="ping-owner", repo="acme/gadgets"),
        _skill_row(signature="ping-owner", repo="other/thing"),
    ]
    raw_sop = "Ping @octocat in acme/widgets when #42 reopens"
    store = RecordingStore(skill_rows=rows)
    insight = _FakeInsightStore({"ping-owner": raw_sop})

    promoted = await promote_global_skills(store, insight, min_repos=3, share_skills=True)
    assert promoted == ["ping-owner"]

    gs_nodes = _nodes_of(store, NodeType.GLOBAL_SKILL.value)
    _, _, props = gs_nodes[0]
    abstracted = props["abstracted_sop_text"]
    # The critical assertion: every raw identifier is gone. Which
    # placeholder lands (``<repo>`` vs ``<path>``) depends on ordering
    # inside the abstraction pass — both are privacy-safe outcomes.
    assert "@octocat" not in abstracted
    assert "acme/widgets" not in abstracted
    assert "#42" not in abstracted
    # At least one placeholder from each scrubbed class is present.
    assert "<user>" in abstracted
    assert "<ref>" in abstracted
    assert ("<repo>" in abstracted) or ("<path>" in abstracted)


@pytest.mark.asyncio
async def test_promote_ignores_unknown_tenant_and_blank_signatures() -> None:
    """Skills missing a tenant or signature aren't eligible for promotion."""
    rows = [
        _skill_row(signature="retry-flaky", repo="acme/widgets"),
        _skill_row(signature="retry-flaky", repo="acme/gadgets"),
        _skill_row(signature="retry-flaky", repo="unknown/unknown"),  # filtered
        _skill_row(signature="", repo="acme/other"),  # filtered
    ]
    store = RecordingStore(skill_rows=rows)
    promoted = await promote_global_skills(store, None, min_repos=3, share_skills=True)
    assert promoted == []  # only 2 real repos, below threshold


@pytest.mark.asyncio
async def test_promote_handles_missing_insight_store() -> None:
    """No insight_store → uses signature as fallback SOP, still redacts."""
    rows = [
        _skill_row(signature="retry-flaky", repo="acme/widgets"),
        _skill_row(signature="retry-flaky", repo="acme/gadgets"),
        _skill_row(signature="retry-flaky", repo="other/thing"),
    ]
    store = RecordingStore(skill_rows=rows)
    promoted = await promote_global_skills(store, None, min_repos=3, share_skills=True)
    assert promoted == ["retry-flaky"]
    gs_nodes = _nodes_of(store, NodeType.GLOBAL_SKILL.value)
    assert gs_nodes[0][2]["abstracted_sop_text"]  # non-empty


# ── FleetConfig wiring ────────────────────────────────────────────────────


def test_fleet_config_defaults_are_safe() -> None:
    """share_skills off by default; min_repos_for_promotion=3."""
    from caretaker.config import FleetConfig, MaintainerConfig

    cfg = MaintainerConfig()
    assert cfg.fleet.share_skills is False
    assert cfg.fleet.min_repos_for_promotion == 3

    explicit = FleetConfig(share_skills=True, min_repos_for_promotion=5)
    assert explicit.share_skills is True
    assert explicit.min_repos_for_promotion == 5


def test_fleet_config_round_trips_through_yaml(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """YAML deserialisation of the new ``fleet:`` block."""
    from caretaker.config import MaintainerConfig

    yaml_path = tmp_path / "config.yml"
    yaml_path.write_text(
        "version: v1\nfleet:\n  share_skills: true\n  min_repos_for_promotion: 5\n"
    )
    cfg = MaintainerConfig.from_yaml(yaml_path)
    assert cfg.fleet.share_skills is True
    assert cfg.fleet.min_repos_for_promotion == 5
