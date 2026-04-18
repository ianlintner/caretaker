"""End-to-end tests for FoundryExecutor.

Wires a real tempdir git repo (via the ``temp_git_repo``/``bare_origin``
fixtures) against a mocked GitHub client and a scripted FakeToolProvider so
the full sequence — pre-flight → workspace → tool_loop → post-flight →
commit → push → result comment — runs without touching the network.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path  # noqa: TC003 — runtime use in fixtures
from unittest.mock import AsyncMock, MagicMock

import pytest

from caretaker.config import FoundryExecutorConfig
from caretaker.foundry.executor import (
    CodingTask,
    ExecutorOutcome,
    FoundryExecutor,
)
from caretaker.github_client.models import Comment, PullRequest, User
from caretaker.llm.copilot import TaskType
from caretaker.llm.provider import LLMToolCall

from .conftest import FakeToolProvider, ScriptedTurn


def _head_sha(repo: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _make_pr(head_sha: str) -> PullRequest:
    return PullRequest(
        number=7,
        title="test",
        body="",
        state="open",
        user=User(login="dev", id=1),
        head_ref="main",  # we'll push to main of the bare origin
        head_sha=head_sha,
        base_ref="main",
    )


def _make_comment() -> Comment:
    return Comment(
        id=1234,
        user=User(login="caretaker-bot", id=99, type="Bot"),
        body="<!-- caretaker:result --> RESULT: FIXED",
        created_at=datetime(2024, 1, 1, tzinfo=UTC),
    )


def _wire_origin(repo: Path, origin: Path) -> None:
    subprocess.run(
        ["git", "remote", "add", "origin", str(origin)],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "push", "origin", "main"],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )


class TestExecutorEndToEnd:
    @pytest.mark.asyncio
    async def test_completes_and_pushes(self, temp_git_repo: Path, bare_origin: Path) -> None:
        _wire_origin(temp_git_repo, bare_origin)
        head = _head_sha(temp_git_repo)

        provider = FakeToolProvider(
            [
                ScriptedTurn(
                    tool_calls=[
                        LLMToolCall(
                            id="c1",
                            name="write_file",
                            arguments={"path": "fix.py", "content": "print('ok')\n"},
                        )
                    ]
                ),
                ScriptedTurn(text="wrote fix.py"),
            ]
        )

        github = MagicMock()
        github.add_issue_comment = AsyncMock(return_value=_make_comment())

        executor = FoundryExecutor(
            provider=provider,
            github=github,
            owner="org",
            repo="repo",
            config=FoundryExecutorConfig(
                enabled=True,
                allowed_task_types=["LINT_FAILURE"],
                allowed_commands=[],
                route_same_repo_only=False,
            ),
            source_repo_path=temp_git_repo,
        )

        # Override the remote URL to point at the bare origin.
        async def _fake_url() -> str:
            return str(bare_origin)

        executor._resolve_remote_url = _fake_url  # type: ignore[method-assign]

        task = CodingTask(
            task_type=TaskType.LINT_FAILURE,
            job_name="lint",
            error_output="E501 long line",
            instructions="fix it",
        )
        result = await executor.run(task, _make_pr(head))

        assert result.outcome == ExecutorOutcome.COMPLETED, result.reason
        assert result.commit_sha is not None
        github.add_issue_comment.assert_awaited_once()
        # Verify the commit landed in the bare origin.
        log = subprocess.run(
            ["git", "log", "--oneline", "main"],
            cwd=str(bare_origin),
            check=True,
            capture_output=True,
            text=True,
        )
        assert "caretaker-foundry" in log.stdout

    @pytest.mark.asyncio
    async def test_escalates_when_no_changes_produced(
        self, temp_git_repo: Path, bare_origin: Path
    ) -> None:
        _wire_origin(temp_git_repo, bare_origin)
        head = _head_sha(temp_git_repo)

        # Model returns final text without any tool calls → no mutations.
        provider = FakeToolProvider([ScriptedTurn(text="nothing to change")])

        github = MagicMock()
        github.add_issue_comment = AsyncMock(return_value=_make_comment())

        executor = FoundryExecutor(
            provider=provider,
            github=github,
            owner="org",
            repo="repo",
            config=FoundryExecutorConfig(
                enabled=True,
                allowed_task_types=["LINT_FAILURE"],
                route_same_repo_only=False,
            ),
            source_repo_path=temp_git_repo,
        )

        task = CodingTask(
            task_type=TaskType.LINT_FAILURE,
            job_name="lint",
            error_output="",
            instructions="",
        )
        result = await executor.run(task, _make_pr(head))

        assert result.outcome == ExecutorOutcome.ESCALATED
        assert "no changes" in result.reason
        github.add_issue_comment.assert_not_called()

    @pytest.mark.asyncio
    async def test_escalates_on_ineligible_task_type(self, temp_git_repo: Path) -> None:
        provider = FakeToolProvider([])
        github = MagicMock()

        executor = FoundryExecutor(
            provider=provider,
            github=github,
            owner="org",
            repo="repo",
            config=FoundryExecutorConfig(
                enabled=True,
                allowed_task_types=["UPGRADE"],  # LINT not allowed
                route_same_repo_only=False,
            ),
            source_repo_path=temp_git_repo,
        )
        task = CodingTask(
            task_type=TaskType.LINT_FAILURE,
            job_name="lint",
            error_output="",
            instructions="",
        )
        result = await executor.run(task, _make_pr("deadbeef"))
        assert result.outcome == ExecutorOutcome.ESCALATED
        assert "allowlist" in result.reason
