"""Tests for the MCP memory adapter (M5 §4.4).

Mirrors the auth-override shortcut used by ``tests/test_fleet_registry.py``:
mount the router on a fresh ``FastAPI`` app and swap
``admin_auth.require_session`` out for a fake dependency so the endpoint
body runs with a stub user identity.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from caretaker.admin import auth as admin_auth
from caretaker.causal_chain import CausalEvent, CausalEventRef, Chain
from caretaker.graph.models import GraphEdge, GraphNode, RelType, SubGraph
from caretaker.mcp_backend import memory_tools


class _FakeCausalStore:
    def __init__(self, events: dict[str, CausalEvent]) -> None:
        self._events = events

    def get(self, event_id: str) -> CausalEvent | None:
        return self._events.get(event_id)

    def walk(self, event_id: str, *, max_depth: int = 50) -> Chain:
        root = self._events.get(event_id)
        if root is None:
            return Chain(events=[], truncated=False)
        return Chain(events=[root], truncated=False)

    def descendants(self, event_id: str, *, max_depth: int = 50) -> list[CausalEvent]:
        return []


@dataclass
class _FakeSkill:
    id: str
    category: str
    signature: str
    sop_text: str
    success_count: int
    fail_count: int

    @property
    def confidence(self) -> float:
        total = self.success_count + self.fail_count
        return 0.0 if total < 3 else self.success_count / total


class _FakeInsightStore:
    def __init__(self, skill: _FakeSkill | None = None) -> None:
        self._skill = skill

    def get_by_signature(self, category: str, signature: str) -> _FakeSkill | None:
        if self._skill is None:
            return None
        if self._skill.category == category and self._skill.signature == signature:
            return self._skill
        return None


class _FakeGraphStore:
    def __init__(self, subgraph: SubGraph) -> None:
        self._subgraph = subgraph

    async def get_neighbors(self, node_id: str, depth: int = 1) -> SubGraph:
        return self._subgraph


def _sample_event() -> CausalEvent:
    return CausalEvent(
        id="evt-1",
        source="pr",
        parent_id=None,
        run_id="r1",
        title="root event",
        observed_at=datetime(2026, 4, 20, tzinfo=UTC),
        ref=CausalEventRef(kind="pr", number=42, owner="acme", repo="widgets"),
    )


def _sample_subgraph() -> SubGraph:
    return SubGraph(
        nodes=[
            GraphNode(id="agent:pr_agent", type="Agent", label="pr_agent"),
            GraphNode(id="acm:pr_agent:r1", type="AgentCoreMemory", label="pr_agent@r1"),
            GraphNode(id="run:2026-04-20", type="Run", label="2026-04-20"),
        ],
        edges=[
            GraphEdge(
                id="e1",
                source="acm:pr_agent:r1",
                target="agent:pr_agent",
                type=RelType.CORE_MEMORY_OF.value,
                properties={"observed_at": "2026-04-20T12:00:00+00:00"},
            ),
            GraphEdge(
                id="e2",
                source="run:2026-04-20",
                target="agent:pr_agent",
                type=RelType.EXECUTED.value,
                properties={"observed_at": "2026-04-20T11:00:00+00:00"},
            ),
            GraphEdge(
                id="e3",
                source="agent:pr_agent",
                target="goal:ci",
                type="CONTRIBUTES_TO",
                properties={"observed_at": "2026-04-20T10:00:00+00:00"},
            ),
        ],
    )


@pytest.fixture
def client_authed():  # type: ignore[no-untyped-def]
    """FastAPI client with all stores configured + require_session bypassed."""
    memory_tools.reset_for_tests()
    memory_tools.configure(
        graph_store=_FakeGraphStore(_sample_subgraph()),  # type: ignore[arg-type]
        causal_store=_FakeCausalStore({"evt-1": _sample_event()}),  # type: ignore[arg-type]
        insight_store=_FakeInsightStore(  # type: ignore[arg-type]
            _FakeSkill(
                id="ci:deadbeef",
                category="ci",
                signature="lint_fix",
                sop_text="Run ruff --fix",
                success_count=8,
                fail_count=2,
            )
        ),
    )

    app = FastAPI()
    app.include_router(memory_tools.router)

    async def _fake_user() -> admin_auth.UserInfo:
        return admin_auth.UserInfo(sub="test", email="t@example.com", name=None, picture=None)

    app.dependency_overrides[admin_auth.require_session] = _fake_user
    yield TestClient(app)
    memory_tools.reset_for_tests()


@pytest.fixture
def client_unauthed():  # type: ignore[no-untyped-def]
    """FastAPI client without the auth override — exercises 401 paths."""
    memory_tools.reset_for_tests()
    memory_tools.configure(
        graph_store=_FakeGraphStore(_sample_subgraph()),  # type: ignore[arg-type]
        causal_store=_FakeCausalStore({"evt-1": _sample_event()}),  # type: ignore[arg-type]
        insight_store=_FakeInsightStore(),  # type: ignore[arg-type]
    )

    app = FastAPI()
    app.include_router(memory_tools.router)
    yield TestClient(app)
    memory_tools.reset_for_tests()


# ── Auth gates ────────────────────────────────────────────────────────────


def test_recent_actions_requires_session(client_unauthed) -> None:  # type: ignore[no-untyped-def]
    resp = client_unauthed.get("/api/mcp/memory/recent-actions", params={"agent": "pr_agent"})
    assert resp.status_code == 401


def test_causal_chain_requires_session(client_unauthed) -> None:  # type: ignore[no-untyped-def]
    resp = client_unauthed.get("/api/mcp/memory/causal-chain/evt-1")
    assert resp.status_code == 401


def test_skill_sop_requires_session(client_unauthed) -> None:  # type: ignore[no-untyped-def]
    resp = client_unauthed.get(
        "/api/mcp/memory/skill-sop", params={"category": "ci", "signature": "lint_fix"}
    )
    assert resp.status_code == 401


# ── Happy paths ───────────────────────────────────────────────────────────


def test_recent_actions_filters_to_core_memory_and_executed(client_authed) -> None:  # type: ignore[no-untyped-def]
    resp = client_authed.get(
        "/api/mcp/memory/recent-actions",
        params={"agent": "pr_agent"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["agent"] == "pr_agent"
    rels = {e["type"] for e in body["edges"]}
    assert rels == {RelType.CORE_MEMORY_OF.value, RelType.EXECUTED.value}
    # Edge e3 (CONTRIBUTES_TO) must be filtered out.
    assert all(e["type"] != "CONTRIBUTES_TO" for e in body["edges"])


def test_recent_actions_since_excludes_older_edges(client_authed) -> None:  # type: ignore[no-untyped-def]
    resp = client_authed.get(
        "/api/mcp/memory/recent-actions",
        params={"agent": "pr_agent", "since": "2026-04-20T11:30:00+00:00"},
    )
    assert resp.status_code == 200
    body = resp.json()
    # Only the CORE_MEMORY_OF edge (12:00) is newer than the cutoff.
    assert [e["type"] for e in body["edges"]] == [RelType.CORE_MEMORY_OF.value]


def test_recent_actions_returns_503_when_graph_store_unset(client_authed) -> None:  # type: ignore[no-untyped-def]
    # Wipe the graph store — the other endpoints should keep working.
    memory_tools.reset_for_tests()
    memory_tools.configure(
        causal_store=_FakeCausalStore({"evt-1": _sample_event()}),  # type: ignore[arg-type]
    )
    resp = client_authed.get("/api/mcp/memory/recent-actions", params={"agent": "pr_agent"})
    assert resp.status_code == 503


def test_causal_chain_returns_chain_and_descendants(client_authed) -> None:  # type: ignore[no-untyped-def]
    resp = client_authed.get("/api/mcp/memory/causal-chain/evt-1")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == "evt-1"
    assert [e["id"] for e in body["chain"]] == ["evt-1"]
    assert body["descendants"] == []
    assert body["truncated"] is False


def test_causal_chain_missing_event_returns_404(client_authed) -> None:  # type: ignore[no-untyped-def]
    resp = client_authed.get("/api/mcp/memory/causal-chain/missing")
    assert resp.status_code == 404


def test_skill_sop_returns_sop_text(client_authed) -> None:  # type: ignore[no-untyped-def]
    resp = client_authed.get(
        "/api/mcp/memory/skill-sop",
        params={"category": "ci", "signature": "lint_fix"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["category"] == "ci"
    assert body["signature"] == "lint_fix"
    assert body["sop_text"] == "Run ruff --fix"
    assert body["success_count"] == 8
    assert body["fail_count"] == 2


def test_skill_sop_missing_returns_404(client_authed) -> None:  # type: ignore[no-untyped-def]
    resp = client_authed.get(
        "/api/mcp/memory/skill-sop",
        params={"category": "ci", "signature": "unknown"},
    )
    assert resp.status_code == 404


def test_skill_sop_503_without_insight_store(client_authed) -> None:  # type: ignore[no-untyped-def]
    memory_tools.reset_for_tests()
    memory_tools.configure(
        graph_store=_FakeGraphStore(_sample_subgraph()),  # type: ignore[arg-type]
        causal_store=_FakeCausalStore({"evt-1": _sample_event()}),  # type: ignore[arg-type]
    )
    resp = client_authed.get(
        "/api/mcp/memory/skill-sop",
        params={"category": "ci", "signature": "lint_fix"},
    )
    assert resp.status_code == 503
