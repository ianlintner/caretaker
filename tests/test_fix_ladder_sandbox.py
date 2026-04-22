"""Tests for :class:`FixLadderSandbox` (Wave A3)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from caretaker.self_heal_agent.sandbox import FixLadderSandbox

if TYPE_CHECKING:
    from pathlib import Path


class TestFixLadderSandbox:
    def test_rejects_nonexistent_working_tree(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="does not exist"):
            FixLadderSandbox(tmp_path / "nope")

    async def test_runs_simple_command_and_captures_stdout(self, tmp_path: Path) -> None:
        sandbox = FixLadderSandbox(tmp_path)
        result = await sandbox.run("echo", ["python3", "-c", "print('hello world')"])
        assert result.exit_code == 0
        assert "hello world" in result.stdout_tail
        assert result.timed_out is False

    async def test_nonzero_exit_is_captured(self, tmp_path: Path) -> None:
        sandbox = FixLadderSandbox(tmp_path)
        result = await sandbox.run(
            "fail",
            ["python3", "-c", "import sys; sys.exit(2)"],
        )
        assert result.exit_code == 2
        assert result.timed_out is False

    async def test_missing_command_returns_127(self, tmp_path: Path) -> None:
        sandbox = FixLadderSandbox(tmp_path)
        result = await sandbox.run("not-real", ["definitely-not-a-real-binary-xyz"])
        assert result.exit_code == 127
        assert result.timed_out is False

    async def test_timeout_surfaces_timed_out_flag(self, tmp_path: Path) -> None:
        sandbox = FixLadderSandbox(tmp_path)
        result = await sandbox.run(
            "slow",
            ["python3", "-c", "import time; time.sleep(5)"],
            timeout_seconds=1,
        )
        assert result.timed_out is True
        assert result.exit_code == -1

    async def test_empty_command_raises(self, tmp_path: Path) -> None:
        sandbox = FixLadderSandbox(tmp_path)
        with pytest.raises(ValueError):
            await sandbox.run("empty", [])

    async def test_output_is_truncated_for_large_streams(self, tmp_path: Path) -> None:
        sandbox = FixLadderSandbox(tmp_path)
        # Generate ~20 KiB of stdout so the tail helper truncates it.
        result = await sandbox.run(
            "big",
            ["python3", "-c", "print('a' * 20000)"],
        )
        assert result.exit_code == 0
        # Tail helper caps at 8 KiB + a "truncated" marker.
        assert len(result.stdout_tail) < 10_000
        assert "truncated" in result.stdout_tail or len(result.stdout_tail) < 8_500
