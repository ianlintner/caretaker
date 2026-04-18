"""Tests for the fork-check path and token-supplier plumbing.

These guard two previously-unexercised paths that the code review flagged:

1. When a PR has ``head_repo_full_name != base_repo_full_name`` (a fork),
   the executor must escalate to Copilot regardless of task eligibility —
   installation tokens can't push to a fork.
2. When no ``token_supplier`` is wired, the executor must fall back to
   ``GITHUB_TOKEN`` from the env; when the env is empty the push path
   must escalate rather than silently succeed with an unusable URL.
"""

from __future__ import annotations

from pathlib import Path  # noqa: TC003 — runtime use in fixtures
from unittest.mock import MagicMock

import pytest

from caretaker.config import FoundryExecutorConfig
from caretaker.foundry.executor import (
    CodingTask,
    ExecutorOutcome,
    FoundryExecutor,
)
from caretaker.github_client.models import PullRequest, User
from caretaker.llm.copilot import TaskType

from .conftest import FakeToolProvider


def _make_fork_pr(head_sha: str = "deadbeef") -> PullRequest:
    return PullRequest(
        number=7,
        title="fork pr",
        body="",
        state="open",
        user=User(login="contributor", id=1),
        head_ref="feature",
        head_sha=head_sha,
        base_ref="main",
        head_repo_full_name="contributor/caretaker",
        base_repo_full_name="ianlintner/caretaker",
    )


def _make_same_repo_pr(head_sha: str = "deadbeef") -> PullRequest:
    return PullRequest(
        number=8,
        title="internal pr",
        body="",
        state="open",
        user=User(login="bot", id=2),
        head_ref="branch",
        head_sha=head_sha,
        base_ref="main",
        head_repo_full_name="ianlintner/caretaker",
        base_repo_full_name="ianlintner/caretaker",
    )


class TestForkCheck:
    @pytest.mark.asyncio
    async def test_fork_pr_escalates_when_route_same_repo_only(
        self, temp_git_repo: Path
    ) -> None:
        """With route_same_repo_only=True, a fork PR must escalate."""
        executor = FoundryExecutor(
            provider=FakeToolProvider([]),
            github=MagicMock(),
            owner="ianlintner",
            repo="caretaker",
            config=FoundryExecutorConfig(
                enabled=True,
                allowed_task_types=["LINT_FAILURE"],
                route_same_repo_only=True,
            ),
            source_repo_path=temp_git_repo,
        )
        task = CodingTask(
            task_type=TaskType.LINT_FAILURE,
            job_name="lint",
            error_output="",
            instructions="",
        )
        result = await executor.run(task, _make_fork_pr())
        assert result.outcome == ExecutorOutcome.ESCALATED
        assert "fork" in result.reason

    @pytest.mark.asyncio
    async def test_same_repo_pr_does_not_escalate_for_fork_reason(
        self, temp_git_repo: Path
    ) -> None:
        """A same-repo PR must pass the fork gate (it can still escalate for
        other reasons, e.g. no-changes — we only assert the reason isn't fork).
        """
        # Use a non-existent SHA so workspace open fails; fork check would
        # have happened first if it were going to fire.
        executor = FoundryExecutor(
            provider=FakeToolProvider([]),
            github=MagicMock(),
            owner="ianlintner",
            repo="caretaker",
            config=FoundryExecutorConfig(
                enabled=True,
                allowed_task_types=["LINT_FAILURE"],
                route_same_repo_only=True,
            ),
            source_repo_path=temp_git_repo,
        )
        task = CodingTask(
            task_type=TaskType.LINT_FAILURE,
            job_name="lint",
            error_output="",
            instructions="",
        )
        result = await executor.run(task, _make_same_repo_pr("deadbeef"))
        # Whatever happened, the reason must not be fork-related.
        assert "fork" not in result.reason.lower()

    @pytest.mark.asyncio
    async def test_route_same_repo_only_false_allows_fork(
        self, temp_git_repo: Path
    ) -> None:
        """With the guard disabled, the pre-flight no longer escalates for
        forks — behavior reverts to eligibility-by-task-type only.
        """
        executor = FoundryExecutor(
            provider=FakeToolProvider([]),
            github=MagicMock(),
            owner="ianlintner",
            repo="caretaker",
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
        result = await executor.run(task, _make_fork_pr())
        # Whatever happens next, it isn't the fork pre-flight.
        assert "fork" not in result.reason.lower()


class TestTokenSupplier:
    @pytest.mark.asyncio
    async def test_missing_token_raises_workspace_error_path(
        self, temp_git_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With no token_supplier AND no GITHUB_TOKEN env var, the push path
        must not silently embed an empty token — _resolve_remote_url must raise.
        """
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        executor = FoundryExecutor(
            provider=FakeToolProvider([]),
            github=MagicMock(),
            owner="org",
            repo="repo",
            config=FoundryExecutorConfig(
                enabled=True,
                allowed_task_types=["LINT_FAILURE"],
                route_same_repo_only=False,
            ),
            source_repo_path=temp_git_repo,
        )
        # The private method is what the push step calls; asserting on it
        # isolates the token-supplier contract from all the other machinery.
        with pytest.raises(Exception) as excinfo:
            await executor._resolve_remote_url()
        assert "token" in str(excinfo.value).lower()

    @pytest.mark.asyncio
    async def test_token_supplier_wins_over_env(
        self, temp_git_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When token_supplier is wired it's preferred over GITHUB_TOKEN."""
        monkeypatch.setenv("GITHUB_TOKEN", "env-token")

        async def _supplier() -> str:
            return "app-token"

        executor = FoundryExecutor(
            provider=FakeToolProvider([]),
            github=MagicMock(),
            owner="org",
            repo="repo",
            config=FoundryExecutorConfig(
                enabled=True,
                allowed_task_types=["LINT_FAILURE"],
                route_same_repo_only=False,
            ),
            source_repo_path=temp_git_repo,
            token_supplier=_supplier,
        )
        url = await executor._resolve_remote_url()
        assert "app-token" in url
        assert "env-token" not in url

    @pytest.mark.asyncio
    async def test_env_fallback_when_no_supplier(
        self, temp_git_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No supplier → GITHUB_TOKEN env var is used."""
        monkeypatch.setenv("GITHUB_TOKEN", "env-token")
        executor = FoundryExecutor(
            provider=FakeToolProvider([]),
            github=MagicMock(),
            owner="org",
            repo="repo",
            config=FoundryExecutorConfig(
                enabled=True,
                allowed_task_types=["LINT_FAILURE"],
                route_same_repo_only=False,
            ),
            source_repo_path=temp_git_repo,
        )
        url = await executor._resolve_remote_url()
        assert "env-token" in url
        assert url.endswith("org/repo.git")
