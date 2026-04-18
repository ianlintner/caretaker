"""Workspace manager — git worktree + safe commit/push for the Foundry executor.

Reuses the repository already checked out in ``$GITHUB_WORKSPACE`` (or a
configured root) via ``git worktree add --detach`` rather than cloning, which
eliminates the largest source of latency and avoids shipping extra credentials.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path  # noqa: TC003 — used at runtime in Workspace ctor

logger = logging.getLogger(__name__)


class WorkspaceError(Exception):
    """Raised when a workspace operation fails."""


@dataclass
class CommitResult:
    """Return value from :meth:`Workspace.commit_all`."""

    sha: str | None  # None when the working tree was clean (nothing to commit)
    files_changed: int
    insertions: int
    deletions: int


class Workspace:
    """An ephemeral git worktree rooted at a PR's head commit.

    Usage::

        async with Workspace(
            source_repo=Path(os.environ["GITHUB_WORKSPACE"]),
            head_sha="abc123",
            author_name="caretaker-bot",
            author_email="caretaker-bot@users.noreply.github.com",
        ) as ws:
            # ws.path is the worktree root
            commit = await ws.commit_all("fix: ruff E501")
            if commit.sha is not None:
                await ws.push(
                    remote_url="https://x-access-token:TOKEN@github.com/org/repo.git",
                    branch="copilot/feat",
                )

    Force-with-lease is used on push so a concurrent write from Copilot fails
    loudly instead of being silently overwritten.
    """

    def __init__(
        self,
        source_repo: Path,
        head_sha: str,
        *,
        author_name: str = "caretaker-bot",
        author_email: str = "caretaker-bot@users.noreply.github.com",
        worktree_parent: Path | None = None,
    ) -> None:
        self._source_repo = Path(source_repo).resolve()
        self._head_sha = head_sha
        self._author_name = author_name
        self._author_email = author_email
        self._worktree_parent = (
            Path(worktree_parent) if worktree_parent else Path(tempfile.gettempdir())
        )
        self._path: Path | None = None
        self._base_sha: str | None = None

    @property
    def path(self) -> Path:
        if self._path is None:
            raise WorkspaceError("Workspace is not open; use 'async with'")
        return self._path

    @property
    def base_sha(self) -> str:
        if self._base_sha is None:
            raise WorkspaceError("Workspace is not open; use 'async with'")
        return self._base_sha

    async def __aenter__(self) -> Workspace:
        if not (self._source_repo / ".git").exists():
            raise WorkspaceError(f"source_repo {self._source_repo} is not a git repository")
        target = self._worktree_parent / f"caretaker-foundry-{uuid.uuid4().hex[:12]}"
        await self._git("worktree", "add", "--detach", str(target), self._head_sha)
        self._path = target
        self._base_sha = self._head_sha
        await self._git("config", "user.name", self._author_name, cwd=target)
        await self._git("config", "user.email", self._author_email, cwd=target)
        logger.info("Foundry workspace opened at %s (base_sha=%s)", target, self._head_sha)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
        if self._path is None:
            return
        try:
            await self._git("worktree", "remove", "--force", str(self._path), check=False)
        except Exception as exc_cleanup:  # best-effort cleanup
            logger.warning("Workspace cleanup failed: %s", exc_cleanup)
        finally:
            self._path = None

    # ── Git operations ───────────────────────────────────────────────

    async def _git(
        self, *args: str, cwd: Path | None = None, check: bool = True
    ) -> tuple[int, str, str]:
        """Run a git command. By default raises on non-zero exit.

        Returns ``(returncode, stdout, stderr)`` regardless.
        """
        cwd_path = cwd if cwd is not None else self._source_repo
        proc = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=str(cwd_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
        stdout, stderr = await proc.communicate()
        out = stdout.decode(errors="replace")
        err = stderr.decode(errors="replace")
        if check and proc.returncode != 0:
            raise WorkspaceError(
                f"git {' '.join(args)} failed (rc={proc.returncode}): {err.strip()}"
            )
        return proc.returncode or 0, out, err

    async def commit_all(self, message: str) -> CommitResult:
        """Stage all changes and commit. Returns a clean result if no changes."""
        if self._path is None:
            raise WorkspaceError("Workspace is not open")
        # ``git add -A`` stages adds, modifies, and deletes.
        await self._git("add", "-A", cwd=self._path)
        rc, stdout, _ = await self._git("diff", "--cached", "--stat", cwd=self._path, check=False)
        if rc != 0 or not stdout.strip():
            return CommitResult(sha=None, files_changed=0, insertions=0, deletions=0)

        await self._git("commit", "-m", message, cwd=self._path)
        _, sha_out, _ = await self._git("rev-parse", "HEAD", cwd=self._path)
        sha = sha_out.strip()

        stats = self._parse_diff_stat(stdout)
        return CommitResult(sha=sha, **stats)

    @staticmethod
    def _parse_diff_stat(stat_output: str) -> dict[str, int]:
        """Parse a ``git diff --stat`` summary line.

        Accepts the trailing line that looks like:
        `` 3 files changed, 10 insertions(+), 2 deletions(-)``
        """
        files_changed = insertions = deletions = 0
        for line in stat_output.splitlines():
            text = line.strip()
            if "file" in text and "changed" in text:
                parts = [p.strip() for p in text.split(",")]
                for part in parts:
                    if "file" in part and "changed" in part:
                        files_changed = int(part.split()[0])
                    elif "insertion" in part:
                        insertions = int(part.split()[0])
                    elif "deletion" in part:
                        deletions = int(part.split()[0])
        return {
            "files_changed": files_changed,
            "insertions": insertions,
            "deletions": deletions,
        }

    async def diff_stat(self) -> dict[str, int]:
        """Return the diff stats of the worktree vs the recorded base SHA."""
        if self._path is None:
            raise WorkspaceError("Workspace is not open")
        _, out, _ = await self._git("diff", "--stat", self.base_sha, cwd=self._path, check=False)
        return self._parse_diff_stat(out)

    async def push(self, *, remote_url: str, branch: str) -> None:
        """Push the worktree HEAD to ``branch`` on the given remote URL.

        Uses ``--force-with-lease=<refname>:<expect>`` with the recorded base
        SHA to guarantee we fail if Copilot (or any other client) pushed to
        the branch in the meantime — never a silent overwrite.
        """
        if self._path is None:
            raise WorkspaceError("Workspace is not open")
        rc, _, err = await self._git(
            "push",
            "--force-with-lease=" + f"refs/heads/{branch}:{self.base_sha}",
            remote_url,
            f"HEAD:refs/heads/{branch}",
            cwd=self._path,
            check=False,
        )
        if rc != 0:
            raise WorkspaceError(
                f"push failed (rc={rc}); likely a concurrent write to {branch}: {err.strip()}"
            )
