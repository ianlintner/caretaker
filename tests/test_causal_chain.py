"""Tests for causal-chain model, extractor, and walkers (Sprint F3)."""

from __future__ import annotations

from caretaker.causal import make_causal_marker
from caretaker.causal_chain import (
    CausalEvent,
    CausalEventRef,
    descendants,
    extract_from_body,
    parse_run_id,
    walk_chain,
)


class TestParseRunId:
    def test_extracts_run_id(self) -> None:
        assert parse_run_id("run-12345-devops") == "12345"

    def test_returns_none_for_local(self) -> None:
        assert parse_run_id("local-abc123-upgrade") is None

    def test_returns_none_for_empty(self) -> None:
        assert parse_run_id("") is None


class TestExtractFromBody:
    def test_returns_none_when_no_marker(self) -> None:
        ref = CausalEventRef(kind="issue", number=1)
        assert extract_from_body("no marker here", ref=ref) is None

    def test_extracts_event(self) -> None:
        marker = make_causal_marker("devops", run_id=42)
        ref = CausalEventRef(kind="issue", number=7, owner="o", repo="r")
        event = extract_from_body(f"## title\n{marker}\nbody text", ref=ref, title="title")
        assert event is not None
        assert event.id == "run-42-devops"
        assert event.source == "devops"
        assert event.run_id == "42"
        assert event.parent_id is None
        assert event.ref == ref
        assert event.title == "title"

    def test_extracts_parent(self) -> None:
        marker = make_causal_marker("issue-agent:dispatch", run_id=99, parent="run-42-devops")
        ref = CausalEventRef(kind="issue", number=10)
        event = extract_from_body(marker, ref=ref)
        assert event is not None
        assert event.parent_id == "run-42-devops"


class TestWalkChain:
    def _make_index(self) -> dict[str, CausalEvent]:
        # devops (root) → issue-dispatch → pr-task → escalation
        events = [
            CausalEvent(id="a", source="devops", parent_id=None, ref=CausalEventRef(kind="issue")),
            CausalEvent(
                id="b",
                source="issue-agent:dispatch",
                parent_id="a",
                ref=CausalEventRef(kind="issue"),
            ),
            CausalEvent(
                id="c",
                source="pr-agent-task",
                parent_id="b",
                ref=CausalEventRef(kind="comment"),
            ),
            CausalEvent(
                id="d",
                source="pr-agent:escalation",
                parent_id="c",
                ref=CausalEventRef(kind="comment"),
            ),
        ]
        return {e.id: e for e in events}

    def test_walk_returns_root_first(self) -> None:
        index = self._make_index()
        chain = walk_chain(index, "d")
        assert [e.id for e in chain.events] == ["a", "b", "c", "d"]
        assert chain.truncated is False

    def test_walk_stops_at_unknown_parent(self) -> None:
        index = self._make_index()
        # b points to "a" which exists; remove "a" to simulate missing ancestor
        del index["a"]
        chain = walk_chain(index, "d")
        assert [e.id for e in chain.events] == ["b", "c", "d"]
        assert chain.truncated is False

    def test_walk_returns_empty_for_unknown_start(self) -> None:
        chain = walk_chain({}, "missing")
        assert chain.events == []
        assert chain.truncated is False

    def test_walk_detects_cycle(self) -> None:
        # a → b → a (cycle)
        events = [
            CausalEvent(id="a", source="x", parent_id="b", ref=CausalEventRef(kind="issue")),
            CausalEvent(id="b", source="y", parent_id="a", ref=CausalEventRef(kind="issue")),
        ]
        index = {e.id: e for e in events}
        chain = walk_chain(index, "b")
        assert chain.truncated is True

    def test_walk_respects_max_depth(self) -> None:
        # Build a long linear chain
        events = [
            CausalEvent(
                id=f"n{i}",
                source="x",
                parent_id=(f"n{i - 1}" if i > 0 else None),
                ref=CausalEventRef(kind="issue"),
            )
            for i in range(10)
        ]
        index = {e.id: e for e in events}
        chain = walk_chain(index, "n9", max_depth=3)
        assert len(chain.events) == 3
        assert chain.truncated is True


class TestDescendants:
    def test_returns_children_and_grandchildren(self) -> None:
        events = [
            CausalEvent(id="a", source="x", parent_id=None, ref=CausalEventRef(kind="issue")),
            CausalEvent(id="b", source="x", parent_id="a", ref=CausalEventRef(kind="issue")),
            CausalEvent(id="c", source="x", parent_id="a", ref=CausalEventRef(kind="issue")),
            CausalEvent(id="d", source="x", parent_id="b", ref=CausalEventRef(kind="issue")),
        ]
        index = {e.id: e for e in events}
        out = descendants(index, "a")
        ids = sorted(e.id for e in out)
        assert ids == ["b", "c", "d"]

    def test_returns_empty_for_unknown(self) -> None:
        assert descendants({}, "missing") == []

    def test_returns_empty_for_leaf(self) -> None:
        events = [CausalEvent(id="a", source="x", parent_id=None, ref=CausalEventRef(kind="issue"))]
        assert descendants({e.id: e for e in events}, "a") == []
