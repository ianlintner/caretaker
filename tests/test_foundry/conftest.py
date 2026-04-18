"""Shared fixtures + helpers for foundry tests."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path  # noqa: TC003 — runtime use in fixtures
from typing import Any

import pytest

from caretaker.foundry.tools import ToolContext
from caretaker.llm.provider import (
    LLMRequest,
    LLMResponse,
    LLMToolCall,
    LLMToolResponse,
)

# ── Temp git repo factory ────────────────────────────────────────────


def _run_git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
    )


@pytest.fixture
def temp_git_repo(tmp_path: Path) -> Path:
    """Initialise a git repo at tmp_path/repo with a single seed commit."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _run_git(repo, "init", "-b", "main")
    _run_git(repo, "config", "user.email", "test@example.com")
    _run_git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("hello\n")
    _run_git(repo, "add", "-A")
    _run_git(repo, "commit", "-m", "initial")
    return repo


@pytest.fixture
def bare_origin(tmp_path: Path) -> Path:
    """Create a bare git repo to use as ``origin`` for push tests."""
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", str(origin)],
        check=True,
        capture_output=True,
    )
    return origin


@pytest.fixture
def tool_ctx(temp_git_repo: Path) -> ToolContext:
    """Default ToolContext rooted at the temp git repo."""
    return ToolContext(
        workspace_root=temp_git_repo,
        write_denylist=[".github/workflows/**", ".caretaker.yml"],
        allowed_commands=["ruff", "echo", "true", "false"],
        command_timeout_seconds=15,
    )


# ── Fake tool-use provider ───────────────────────────────────────────


@dataclass
class ScriptedTurn:
    """A single scripted response from :class:`FakeToolProvider`.

    If ``tool_calls`` is empty, the turn is treated as the final assistant
    message and terminates the loop.
    """

    text: str = ""
    tool_calls: list[LLMToolCall] = field(default_factory=list)
    input_tokens: int = 5
    output_tokens: int = 3


class FakeToolProvider:
    """Stub of :class:`LLMProvider` for tool-use loop tests.

    Each call to :meth:`complete_with_tools` pops the next scripted turn in
    FIFO order and returns it.  :meth:`complete` is a no-op returning an
    empty response.
    """

    name = "fake-tool-provider"
    available = True

    def __init__(self, script: list[ScriptedTurn]) -> None:
        self._script = list(script)
        self.calls: list[LLMRequest] = []

    async def complete(self, request: LLMRequest) -> LLMResponse:
        return LLMResponse(text="", model=request.model, provider=self.name)

    async def complete_with_tools(
        self, request: LLMRequest, tools: list[dict[str, Any]]
    ) -> LLMToolResponse:
        self.calls.append(request)
        if not self._script:
            # Safety: if tests under-script, return a terminating empty turn.
            return LLMToolResponse(
                text="(no more scripted turns)",
                tool_calls=[],
                model=request.model,
                provider=self.name,
            )
        turn = self._script.pop(0)
        return LLMToolResponse(
            text=turn.text,
            tool_calls=turn.tool_calls,
            model=request.model,
            provider=self.name,
            input_tokens=turn.input_tokens,
            output_tokens=turn.output_tokens,
            raw_message={
                "role": "assistant",
                "content": turn.text or None,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.name, "arguments": "{}"},
                    }
                    for tc in turn.tool_calls
                ]
                or None,
            },
        )


# ── Helpers ──────────────────────────────────────────────────────────


def run_sync(coro: Any) -> Any:
    """Run an async coroutine in a fresh event loop (for non-async tests)."""
    return asyncio.get_event_loop().run_until_complete(coro)


def cleanup_path(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)


# Silence pytest-asyncio's deprecation by ensuring we have an event loop fixture
# scoped appropriately. The project's conftest doesn't pin a policy, so we rely
# on pytest-asyncio's default loop per-test.

__all__ = [
    "FakeToolProvider",
    "ScriptedTurn",
    "bare_origin",
    "cleanup_path",
    "run_sync",
    "temp_git_repo",
    "tool_ctx",
]


# Silence lint about unused tempfile import (used conditionally below).
_ = tempfile
