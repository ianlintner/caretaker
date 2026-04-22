"""Tests for the deterministic-first fix ladder (Wave A3).

Covers:

* Unit tests on :func:`_rung_matches` and signature gating
* Integration test: incident → ladder → non-empty patch, outcome=fixed
* Escalation-prompt contents (error_sig, rungs tried, past incidents)
* Metrics sink emissions
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pytest

from caretaker.self_heal_agent.fix_ladder import (
    DEFAULT_RUNGS,
    FixLadderResult,
    FixLadderRung,
    Incident,
    _extract_resolution_package,
    _rung_matches,
    run_fix_ladder,
)
from caretaker.self_heal_agent.sandbox import RungExecution

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

# ── Fakes ────────────────────────────────────────────────────────────────


@dataclass
class _FakeSandbox:
    """Test double for :class:`FixLadderSandbox`.

    ``scripted`` maps rung name → (exit_code, stdout, stderr, mutates_tree).
    When ``mutates_tree`` is True the next ``diff_fn`` call returns a
    non-empty patch — this lets tests simulate "ruff-format produced a
    diff" without touching real files.
    """

    working_tree: Path
    scripted: dict[str, tuple[int, str, str, bool]]
    ran: list[str]
    _diff_bumps: int = 0

    async def run(
        self,
        name: str,
        command: list[str],
        *,
        timeout_seconds: int = 120,
    ) -> RungExecution:
        self.ran.append(name)
        scripted = self.scripted.get(name, (0, "", "", False))
        exit_code, stdout, stderr, mutates = scripted
        if mutates:
            self._diff_bumps += 1
        return RungExecution(
            name=name,
            command=list(command),
            exit_code=exit_code,
            stdout_tail=stdout,
            stderr_tail=stderr,
            duration_seconds=0.01,
            timed_out=False,
        )


def _make_diff_fn(sandbox: _FakeSandbox) -> Callable[[Path | str], str]:
    """Return a diff fn whose output grows each time ``sandbox._diff_bumps`` increments."""
    last_seen = {"n": 0}

    def _diff(_path: Path | str) -> str:
        if sandbox._diff_bumps > last_seen["n"]:
            last_seen["n"] = sandbox._diff_bumps
            return "diff --git a/foo.py b/foo.py\n--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n-x\n+x\n"
        return "diff --git a/foo.py b/foo.py\n" if sandbox._diff_bumps > 0 else ""

    return _diff


class _FakeRetriever:
    """Minimal stand-in for :class:`MemoryRetriever`."""

    def __init__(self, hits: list[Any] | None = None) -> None:
        self._hits = hits or []
        self.find_calls: list[tuple[str, str, str | None]] = []

    async def find_relevant(
        self,
        agent: str,
        query_text: str,
        *,
        repo_slug: str | None = None,
    ) -> list[Any]:
        self.find_calls.append((agent, query_text, repo_slug))
        return self._hits

    def format_for_prompt(self, hits: list[Any]) -> str:
        return "## Relevant past runs\n" + "\n".join(
            f"- past: {getattr(h, 'summary', '')}" for h in hits
        )


# ── Signature gating ─────────────────────────────────────────────────────


class TestRungMatching:
    def test_rung_with_no_gate_matches_every_incident(self) -> None:
        rung = FixLadderRung(name="anything", command=["true"])
        incident = Incident(
            error_signature="abc",
            kind="unknown",
            log_tail="whatever",
            repo_slug="o/r",
            job_name="ci",
        )
        assert _rung_matches(rung, incident) is True

    def test_gate_matches_log_tail(self) -> None:
        rung = DEFAULT_RUNGS[0]  # ruff-format
        incident = Incident(
            error_signature="sig",
            kind="unknown",
            log_tail="fix end of files by running prettier",
            repo_slug="o/r",
            job_name="lint",
        )
        assert rung.name == "ruff-format"
        assert _rung_matches(rung, incident) is True

    def test_gate_does_not_match_unrelated_log(self) -> None:
        rung = DEFAULT_RUNGS[0]  # ruff-format
        incident = Incident(
            error_signature="sig",
            kind="unknown",
            log_tail="AttributeError in agent.py",
            repo_slug="o/r",
            job_name="test",
        )
        assert _rung_matches(rung, incident) is False

    def test_invalid_regex_is_silently_skipped(self) -> None:
        rung = FixLadderRung(
            name="bad",
            command=["true"],
            only_if_signature_matches=[r"(unterminated["],
        )
        incident = Incident(
            error_signature="x",
            kind="unknown",
            log_tail="y",
            repo_slug="o/r",
            job_name="j",
        )
        # No match ever fires because the regex is malformed.
        assert _rung_matches(rung, incident) is False

    def test_pip_compile_extracts_package_from_log(self) -> None:
        log = (
            "ERROR: Could not find a version that satisfies the requirement numpy==99.0.0 "
            "(from versions: 1.0, 1.1)"
        )
        assert _extract_resolution_package(log) == "numpy==99.0.0"


# ── Integration: ladder end-to-end ───────────────────────────────────────


@pytest.fixture
def working_tree(tmp_path: Path) -> Path:
    (tmp_path / "foo.py").write_text("x\n")
    return tmp_path


class TestLadderOutcomes:
    async def test_ladder_fixed_when_matching_rung_produces_diff(self, working_tree: Path) -> None:
        sandbox = _FakeSandbox(
            working_tree=working_tree,
            scripted={"ruff-format": (0, "", "", True)},
            ran=[],
        )
        incident = Incident(
            error_signature="trailing",
            kind="unknown",
            log_tail="fix end of files and trailing whitespace",
            repo_slug="o/r",
            job_name="lint",
        )
        metrics: list[tuple[str, str]] = []
        result = await run_fix_ladder(
            incident,
            sandbox=sandbox,  # type: ignore[arg-type]
            rungs=[DEFAULT_RUNGS[0]],  # only ruff-format
            diff_fn=_make_diff_fn(sandbox),
            metrics_sink=lambda r, o: metrics.append((r, o)),
        )
        assert result.outcome == "fixed"
        assert result.patch is not None and "foo.py" in result.patch
        assert result.winning_rung == "ruff-format"
        assert result.commit_message is not None
        assert "ruff-format" in result.commit_message
        assert ("ruff-format", "progress") in metrics

    async def test_ladder_no_op_when_no_rung_matches(self, working_tree: Path) -> None:
        sandbox = _FakeSandbox(working_tree=working_tree, scripted={}, ran=[])
        incident = Incident(
            error_signature="deadbeef",
            kind="unknown",
            log_tail="completely unrelated failure",
            repo_slug="o/r",
            job_name="j",
        )
        result = await run_fix_ladder(
            incident,
            sandbox=sandbox,  # type: ignore[arg-type]
            diff_fn=lambda _p: "",
        )
        assert result.outcome == "no_op"
        assert sandbox.ran == []

    async def test_ladder_escalated_when_rung_makes_no_progress(self, working_tree: Path) -> None:
        sandbox = _FakeSandbox(
            working_tree=working_tree,
            scripted={"ruff-format": (1, "", "still failing", False)},
            ran=[],
        )
        incident = Incident(
            error_signature="sig123",
            kind="lint_failure",
            log_tail="Would reformat: src/foo.py",
            repo_slug="o/r",
            job_name="lint",
        )
        escalations: list[tuple[str, str]] = []
        result = await run_fix_ladder(
            incident,
            sandbox=sandbox,  # type: ignore[arg-type]
            rungs=[DEFAULT_RUNGS[0]],
            diff_fn=lambda _p: "",
            escalation_metrics_sink=lambda r, s: escalations.append((r, s)),
        )
        assert result.outcome == "escalated"
        assert result.escalation_prompt is not None
        assert "sig123" in result.escalation_prompt
        assert "ruff-format" in result.escalation_prompt
        assert escalations == [("o/r", "sig123")]

    async def test_escalation_prompt_embeds_retriever_hits(self, working_tree: Path) -> None:
        sandbox = _FakeSandbox(
            working_tree=working_tree,
            scripted={"ruff-format": (1, "", "still failing", False)},
            ran=[],
        )
        incident = Incident(
            error_signature="siglong",
            kind="lint_failure",
            log_tail="Would reformat: src/foo.py",
            repo_slug="o/r",
            job_name="lint",
        )

        @dataclass
        class _Hit:
            summary: str = "past lint failure in foo.py"

        retriever = _FakeRetriever(hits=[_Hit(), _Hit(summary="another one")])
        result = await run_fix_ladder(
            incident,
            sandbox=sandbox,  # type: ignore[arg-type]
            rungs=[DEFAULT_RUNGS[0]],
            memory_retriever=retriever,  # type: ignore[arg-type]
            diff_fn=lambda _p: "",
        )
        assert result.outcome == "escalated"
        assert result.escalation_prompt is not None
        # Error sig present
        assert "siglong" in result.escalation_prompt
        # Rungs tried present
        assert "ruff-format" in result.escalation_prompt
        # Retriever hits present
        assert "past lint failure in foo.py" in result.escalation_prompt
        # CLAUDE.md norms pointer present
        assert "CLAUDE.md" in result.escalation_prompt
        # Retriever was called with the right scope
        assert retriever.find_calls[0][2] == "o/r"

    async def test_ladder_caps_pytest_lastfail_on_large_failure_set(
        self, working_tree: Path
    ) -> None:
        log = "\n".join(f"FAILED tests/test_a.py::test_{i}" for i in range(10))
        sandbox = _FakeSandbox(
            working_tree=working_tree,
            scripted={"pytest-lastfail": (0, "", "", True)},
            ran=[],
        )
        incident = Incident(
            error_signature="many",
            kind="test_failure",
            log_tail=log,
            repo_slug="o/r",
            job_name="test",
        )
        result = await run_fix_ladder(
            incident,
            sandbox=sandbox,  # type: ignore[arg-type]
            rungs=[DEFAULT_RUNGS[4]],  # pytest-lastfail
            diff_fn=lambda _p: "",
        )
        assert result.outcome == "no_op"  # refused to run
        assert sandbox.ran == []

    async def test_ladder_error_when_sandbox_raises(self, working_tree: Path) -> None:
        class _BoomSandbox:
            working_tree = working_tree

            async def run(self, *_args: Any, **_kwargs: Any) -> RungExecution:
                raise RuntimeError("sandbox boom")

        incident = Incident(
            error_signature="trailing",
            kind="lint",
            log_tail="fix end of files",
            repo_slug="o/r",
            job_name="j",
        )
        result = await run_fix_ladder(
            incident,
            sandbox=_BoomSandbox(),  # type: ignore[arg-type]
            rungs=[DEFAULT_RUNGS[0]],
            diff_fn=lambda _p: "",
        )
        assert result.outcome == "error"

    async def test_ladder_outcome_record_is_pydantic_serialisable(self, working_tree: Path) -> None:
        sandbox = _FakeSandbox(
            working_tree=working_tree,
            scripted={"ruff-format": (0, "ok", "", True)},
            ran=[],
        )
        incident = Incident(
            error_signature="roundtrip",
            kind="lint",
            log_tail="Would reformat: a.py",
            repo_slug="o/r",
            job_name="lint",
        )
        result = await run_fix_ladder(
            incident,
            sandbox=sandbox,  # type: ignore[arg-type]
            rungs=[DEFAULT_RUNGS[0]],
            diff_fn=_make_diff_fn(sandbox),
        )
        # Pydantic round-trip — proves the record schema is stable for
        # persistence callers (graph writer, escalation issue body).
        as_json = result.model_dump_json()
        rebuilt = FixLadderResult.model_validate_json(as_json)
        assert rebuilt.outcome == result.outcome
        assert len(rebuilt.rungs_run) == len(result.rungs_run)
