"""Tests for causal-token marker helpers (Sprint B3)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from caretaker.causal import extract_causal, make_causal_id, make_causal_marker

if TYPE_CHECKING:
    import pytest


class TestMakeCausalId:
    def test_uses_explicit_run_id(self) -> None:
        cid = make_causal_id("pr-agent", run_id=12345)
        assert cid == "run-12345-pr-agent"

    def test_reads_github_run_id_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GITHUB_RUN_ID", "42")
        assert make_causal_id("self-heal") == "run-42-self-heal"

    def test_falls_back_to_local_uuid(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("GITHUB_RUN_ID", raising=False)
        cid = make_causal_id("upgrade")
        assert cid.startswith("local-")
        assert cid.endswith("-upgrade")

    def test_explicit_run_id_overrides_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GITHUB_RUN_ID", "999")
        assert make_causal_id("x", run_id="7") == "run-7-x"


class TestMakeCausalMarker:
    def test_minimal_has_id_and_source(self) -> None:
        m = make_causal_marker("pr-agent", run_id=1)
        assert m == "<!-- caretaker:causal id=run-1-pr-agent source=pr-agent -->"

    def test_includes_parent_when_given(self) -> None:
        m = make_causal_marker("self-heal", run_id=2, parent="run-1-pr-agent")
        assert (
            m
            == "<!-- caretaker:causal id=run-2-self-heal source=self-heal parent=run-1-pr-agent -->"
        )

    def test_explicit_causal_id_used_verbatim(self) -> None:
        m = make_causal_marker("x", causal_id="custom-abc")
        assert "id=custom-abc" in m
        assert "source=x" in m

    def test_matches_dispatch_guard_regex(self) -> None:
        """Marker must still match the workflow JS self-trigger guard regex.

        Dispatch guard in .github/workflows/maintainer.yml uses
        ``/<!--\\s*caretaker:[a-z0-9:_-]+/i`` to detect any caretaker marker.
        The causal marker has to keep matching so the guard still trips.
        """
        import re as _re

        guard_re = _re.compile(r"<!--\s*caretaker:[a-z0-9:_-]+", _re.IGNORECASE)
        m = make_causal_marker("pr-agent", run_id=1)
        assert guard_re.search(m) is not None


class TestExtractCausal:
    def test_returns_none_when_absent(self) -> None:
        assert extract_causal("no marker here") is None
        assert extract_causal("") is None

    def test_round_trip_minimal(self) -> None:
        m = make_causal_marker("pr-agent", run_id=10)
        assert extract_causal(m) == {"id": "run-10-pr-agent", "source": "pr-agent"}

    def test_round_trip_with_parent(self) -> None:
        m = make_causal_marker("self-heal", run_id=20, parent="run-10-pr-agent")
        assert extract_causal(m) == {
            "id": "run-20-self-heal",
            "source": "self-heal",
            "parent": "run-10-pr-agent",
        }

    def test_returns_first_marker_in_body(self) -> None:
        body = (
            "## [Maintainer] Upgrade to v1.0\n"
            "<!-- caretaker:upgrade target=1.0 -->\n"
            "<!-- caretaker:causal id=run-5-upgrade source=upgrade -->\n"
            "more body"
        )
        assert extract_causal(body) == {"id": "run-5-upgrade", "source": "upgrade"}

    def test_tolerates_extra_whitespace(self) -> None:
        raw = "<!--   caretaker:causal   id=abc   source=x   parent=y   -->"
        assert extract_causal(raw) == {"id": "abc", "source": "x", "parent": "y"}
