"""Tests for the Workspace context manager (git worktree + push)."""

from __future__ import annotations

import subprocess
from pathlib import Path  # noqa: TC003 — runtime use in tests

import pytest

from caretaker.foundry.workspace import Workspace, WorkspaceError


def _head_sha(repo: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


class TestWorkspaceLifecycle:
    @pytest.mark.asyncio
    async def test_open_creates_worktree(self, temp_git_repo: Path) -> None:
        head = _head_sha(temp_git_repo)
        async with Workspace(source_repo=temp_git_repo, head_sha=head) as ws:
            assert ws.path.exists()
            assert (ws.path / "README.md").is_file()
            assert ws.base_sha == head

    @pytest.mark.asyncio
    async def test_cleanup_removes_worktree(self, temp_git_repo: Path) -> None:
        head = _head_sha(temp_git_repo)
        ws_path: Path
        async with Workspace(source_repo=temp_git_repo, head_sha=head) as ws:
            ws_path = ws.path
            assert ws_path.exists()
        assert not ws_path.exists()

    @pytest.mark.asyncio
    async def test_raises_for_non_git_source(self, tmp_path: Path) -> None:
        with pytest.raises(WorkspaceError):
            async with Workspace(source_repo=tmp_path, head_sha="abc"):
                pass


class TestCommitAll:
    @pytest.mark.asyncio
    async def test_no_changes_returns_none_sha(self, temp_git_repo: Path) -> None:
        head = _head_sha(temp_git_repo)
        async with Workspace(source_repo=temp_git_repo, head_sha=head) as ws:
            result = await ws.commit_all("nothing to commit")
            assert result.sha is None
            assert result.files_changed == 0

    @pytest.mark.asyncio
    async def test_writes_and_commits(self, temp_git_repo: Path) -> None:
        head = _head_sha(temp_git_repo)
        async with Workspace(source_repo=temp_git_repo, head_sha=head) as ws:
            (ws.path / "new.txt").write_text("hello foundry\n")
            result = await ws.commit_all("feat: add new.txt")
            assert result.sha is not None
            assert result.sha != head
            assert result.files_changed == 1
            assert result.insertions >= 1


class TestPushWithLease:
    @pytest.mark.asyncio
    async def test_push_to_bare_origin_succeeds(
        self, temp_git_repo: Path, bare_origin: Path
    ) -> None:
        # Wire origin to a bare repo and push main so the ref exists.
        subprocess.run(
            ["git", "remote", "add", "origin", str(bare_origin)],
            cwd=str(temp_git_repo),
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "push", "origin", "main"],
            cwd=str(temp_git_repo),
            check=True,
            capture_output=True,
        )
        head = _head_sha(temp_git_repo)

        async with Workspace(source_repo=temp_git_repo, head_sha=head) as ws:
            (ws.path / "fix.txt").write_text("fix\n")
            commit = await ws.commit_all("fix: foundry")
            assert commit.sha is not None
            await ws.push(remote_url=str(bare_origin), branch="main")

        # Verify the bare origin now has the new commit.
        log = subprocess.run(
            ["git", "log", "--oneline", "main"],
            cwd=str(bare_origin),
            check=True,
            capture_output=True,
            text=True,
        )
        assert "fix: foundry" in log.stdout

    @pytest.mark.asyncio
    async def test_push_fails_when_remote_raced(
        self, temp_git_repo: Path, bare_origin: Path
    ) -> None:
        subprocess.run(
            ["git", "remote", "add", "origin", str(bare_origin)],
            cwd=str(temp_git_repo),
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "push", "origin", "main"],
            cwd=str(temp_git_repo),
            check=True,
            capture_output=True,
        )
        stale_head = _head_sha(temp_git_repo)

        # Simulate a concurrent writer advancing origin past the base SHA.
        (temp_git_repo / "concurrent.txt").write_text("race\n")
        subprocess.run(
            ["git", "add", "-A"], cwd=str(temp_git_repo), check=True, capture_output=True
        )
        subprocess.run(
            ["git", "commit", "-m", "race"],
            cwd=str(temp_git_repo),
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "push", "origin", "main"],
            cwd=str(temp_git_repo),
            check=True,
            capture_output=True,
        )

        async with Workspace(source_repo=temp_git_repo, head_sha=stale_head) as ws:
            (ws.path / "foundry.txt").write_text("foundry\n")
            await ws.commit_all("foundry: fix")
            with pytest.raises(WorkspaceError, match="push failed"):
                await ws.push(remote_url=str(bare_origin), branch="main")
