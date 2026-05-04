"""Shared clone-and-checkout helper for backends that need a local copy of a PR.

Both ``claude_code_local`` (review) and the auto-fix dispatcher (fix
mode) clone the PR's repo into a temp dir, do work, then clean up.
Keeping this in one place means review and fix flows can't drift in
how they prepare the workdir (e.g. shallow depth, ref-fetch strategy)
and a future k8s-Job mode can swap one helper instead of two.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import tempfile
import urllib.parse
from dataclasses import dataclass

from caretaker.pr_reviewer.backends._subprocess_streaming import stream_subprocess_output

logger = logging.getLogger(__name__)


class WorkdirError(RuntimeError):
    """Raised when the clone/checkout machinery fails."""


@dataclass(frozen=True)
class ParsedPRURL:
    owner: str
    repo: str
    number: int


def parse_pr_url(pr_url: str) -> ParsedPRURL:
    """Extract owner/repo/PR number from a github PR URL.

    Accepts both ``https://github.com/owner/repo/pull/N`` (browser URL)
    and ``https://api.github.com/repos/owner/repo/pulls/N`` (API URL).
    """
    parsed = urllib.parse.urlparse(pr_url)
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) >= 4 and parts[-2] in {"pull", "pulls"}:
        return ParsedPRURL(owner=parts[-4], repo=parts[-3], number=int(parts[-1]))
    if len(parts) >= 5 and parts[0] == "repos" and parts[-2] in {"pull", "pulls"}:
        return ParsedPRURL(owner=parts[1], repo=parts[2], number=int(parts[-1]))
    raise WorkdirError(f"cannot parse PR URL: {pr_url!r}")


def clone_url(parsed: ParsedPRURL, *, github_token: str | None) -> str:
    """Build the HTTPS clone URL, embedding the token when present.

    Token-embedded clone is the standard pattern for GitHub Actions
    runners; the token never lands on disk because git only uses it
    for the HTTP exchange.
    """
    if github_token:
        return f"https://x-access-token:{github_token}@github.com/{parsed.owner}/{parsed.repo}.git"
    return f"https://github.com/{parsed.owner}/{parsed.repo}.git"


async def _run_git(
    *args: str,
    cwd: str | None = None,
    timeout: int = 120,
    env: dict[str, str] | None = None,
) -> str:
    """Run ``git <args>``, stream output, raise ``WorkdirError`` on failure."""
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        stdout, stderr = await stream_subprocess_output(
            proc,
            timeout_seconds=timeout,
            stdout_log=lambda line: logger.info("git | %s", line),
            stderr_log=lambda line: logger.info("git! %s", line),  # git uses stderr for progress
        )
    except TimeoutError as exc:
        raise WorkdirError(f"git {args[0]} timed out after {timeout}s") from exc
    if proc.returncode != 0:
        safe_args = [re.sub(r"x-access-token:[^@]+@", "x-access-token:***@", a) for a in args]
        raise WorkdirError(
            f"git {' '.join(safe_args)} exited {proc.returncode}: "
            f"{stderr.strip() or stdout.strip()[:500]}"
        )
    return stdout


async def prepare_workdir(
    pr_url: str,
    *,
    clone_depth: int = 50,
    workdir_root: str | None = None,
    head_branch: str | None = None,
    github_token: str | None = None,
) -> tuple[str, ParsedPRURL]:
    """Clone the repo + check out the PR head into a fresh temp dir.

    When ``head_branch`` is provided (auto-fix flow needs to push back
    to that branch), the clone fetches that ref into a tracking branch
    named identically — so a later ``git push HEAD:<head_branch>``
    targets the right destination. When ``head_branch`` is None
    (read-only review flow), the PR head is fetched as
    ``caretaker/pr-head`` for clarity in logs.

    Returns ``(repo_dir, parsed)``. Caller is responsible for cleanup
    via :func:`cleanup_workdir`.
    """
    parsed = parse_pr_url(pr_url)
    workdir = tempfile.mkdtemp(
        prefix=f"caretaker-pr-{parsed.repo}-{parsed.number}-",
        dir=workdir_root or None,
    )
    logger.info(
        "workdir: %s for %s/%s#%d (head_branch=%s)",
        workdir,
        parsed.owner,
        parsed.repo,
        parsed.number,
        head_branch or "<sha-only>",
    )

    repo_dir = os.path.join(workdir, "repo")
    await _run_git(
        "clone",
        "--depth",
        str(clone_depth),
        "--quiet",
        clone_url(parsed, github_token=github_token),
        repo_dir,
    )

    pr_ref = f"refs/pull/{parsed.number}/head"
    local_branch = head_branch or "caretaker/pr-head"
    await _run_git("fetch", "origin", f"{pr_ref}:{local_branch}", cwd=repo_dir)
    await _run_git("checkout", local_branch, cwd=repo_dir)
    return repo_dir, parsed


def cleanup_workdir(repo_dir: str, *, keep: bool = False) -> None:
    """Remove the workdir unless the operator asked to keep it for debugging."""
    if keep:
        logger.warning("workdir: keeping %s for inspection", repo_dir)
        return
    parent = os.path.dirname(repo_dir.rstrip("/"))
    shutil.rmtree(parent, ignore_errors=True)


__all__ = [
    "ParsedPRURL",
    "WorkdirError",
    "cleanup_workdir",
    "clone_url",
    "parse_pr_url",
    "prepare_workdir",
]
