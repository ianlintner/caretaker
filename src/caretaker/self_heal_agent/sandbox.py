"""Lightweight subprocess sandbox for fix-ladder rungs.

Wave A3 of the R&D plan. The fix ladder (see :mod:`fix_ladder`) needs
to execute short-lived deterministic commands (``ruff format``, ``ruff
check --fix``, ``mypy --install-types``, ``pip-compile``,
``pytest --lf -x``) against a working tree and capture their exit
code + bounded output. The existing :class:`FoundryExecutor` is
designed for multi-minute LLM-driven sessions — heavier than we need
here. This module ships the lighter alternative the brief calls out:
a plain ``subprocess.run`` wrapper with wall-clock timeout and
per-stream output caps.

Design notes
------------

* The sandbox is a thin shim — no container runtime, no chroot. The
  caller is expected to pass a path they already trust (typically a
  git worktree the orchestrator prepared for the dispatch). All rungs
  are signature-gated so the ladder never runs an arbitrary command
  against a path it didn't vet first.
* Stdout / stderr are captured with a hard byte cap (``_OUTPUT_CAP``
  default 8 KiB each). Longer streams are truncated to the tail —
  the diagnostic signal on compiler / lint output is nearly always
  at the end.
* Execution is synchronous-but-offloaded. The fix-ladder runner is
  an async coroutine so we push the blocking ``subprocess.run`` onto
  the default thread executor via ``asyncio.to_thread``. Keeps the
  dispatch event loop responsive even when a rung hits its timeout.
* The sandbox never raises on non-zero exit codes — the caller
  inspects :class:`RungExecution.exit_code` to decide whether a rung
  made progress. Timeouts surface as ``exit_code = -1`` and
  ``timed_out = True`` so the caller can distinguish "command said no"
  from "command never answered".
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Bytes retained per stream. Doubled from the 4 KiB CI-triage log tail
# because rung runners (mypy, pytest) sometimes emit a few diagnostic
# paragraphs before the actual error line. 8 KiB fits comfortably
# inside the LLM prompt budget when the escalation path picks up.
_OUTPUT_CAP = 8 * 1024


@dataclass
class RungExecution:
    """Result of one sandbox invocation.

    Mirrors the subset of ``subprocess.CompletedProcess`` that the
    fix ladder actually reasons about, plus a ``timed_out`` flag so
    callers don't have to re-derive it from the exit code.
    """

    name: str
    command: list[str]
    exit_code: int
    stdout_tail: str
    stderr_tail: str
    duration_seconds: float
    timed_out: bool = False


def _tail_bytes(data: bytes, cap: int = _OUTPUT_CAP) -> str:
    """Return the last ``cap`` bytes of ``data`` decoded as UTF-8."""
    if len(data) <= cap:
        return data.decode("utf-8", errors="replace")
    # Keep the tail — diagnostic signal is at the end for compilers,
    # linters, and test runners. Prepend a marker so readers know the
    # stream was truncated.
    return "…[truncated]…\n" + data[-cap:].decode("utf-8", errors="replace")


class FixLadderSandbox:
    """Subprocess sandbox with bounded output capture.

    Args:
        working_tree: Path to the repo checkout. All rung commands
            run with this directory as their CWD. The caller is
            responsible for staging the working tree — the sandbox
            does not clone, fetch, or reset.
        env: Optional environment override. When ``None`` the current
            process environment is inherited (so subprocesses can see
            ``PATH``, ``VIRTUAL_ENV``, etc.); callers that need
            hermetic execution should pass their own dict.

    The constructor validates that ``working_tree`` exists and is a
    directory — failing early beats surfacing ``FileNotFoundError``
    once per rung.
    """

    def __init__(
        self,
        working_tree: Path | str,
        *,
        env: dict[str, str] | None = None,
    ) -> None:
        self._working_tree = Path(working_tree).resolve()
        if not self._working_tree.is_dir():
            raise ValueError(
                f"FixLadderSandbox: working_tree does not exist or is not a directory: "
                f"{self._working_tree}"
            )
        self._env = env

    @property
    def working_tree(self) -> Path:
        return self._working_tree

    async def run(
        self,
        name: str,
        command: list[str],
        *,
        timeout_seconds: int = 120,
    ) -> RungExecution:
        """Execute ``command`` in the working tree and return the result.

        Runs in a thread via :func:`asyncio.to_thread` so the dispatch
        event loop stays responsive while long-running rungs (pytest,
        pip-compile) complete. The timeout applies wall-clock; on
        expiry the child is hard-killed and the returned
        :class:`RungExecution` has ``timed_out=True`` and
        ``exit_code=-1``.
        """
        if not command:
            raise ValueError(f"FixLadderSandbox.run: rung '{name}' has empty command")
        return await asyncio.to_thread(self._run_sync, name, command, timeout_seconds)

    def _run_sync(
        self,
        name: str,
        command: list[str],
        timeout_seconds: int,
    ) -> RungExecution:
        import time

        start = time.monotonic()
        try:
            completed = subprocess.run(  # noqa: S603 - rung commands are curated
                command,
                cwd=str(self._working_tree),
                capture_output=True,
                timeout=max(1, timeout_seconds),
                env=self._env,
                check=False,
            )
            duration = time.monotonic() - start
            return RungExecution(
                name=name,
                command=list(command),
                exit_code=completed.returncode,
                stdout_tail=_tail_bytes(completed.stdout or b""),
                stderr_tail=_tail_bytes(completed.stderr or b""),
                duration_seconds=duration,
                timed_out=False,
            )
        except subprocess.TimeoutExpired as exc:
            duration = time.monotonic() - start
            # ``exc.stdout`` / ``exc.stderr`` may be ``None`` when the
            # child was killed before any bytes were emitted. Coerce
            # to empty bytes so the tail helper never sees ``None``.
            stdout_tail = _tail_bytes((exc.stdout or b"") if isinstance(exc.stdout, bytes) else b"")
            stderr_tail = _tail_bytes((exc.stderr or b"") if isinstance(exc.stderr, bytes) else b"")
            logger.info(
                "FixLadderSandbox: rung %s timed out after %.1fs (command=%s)",
                name,
                duration,
                command,
            )
            return RungExecution(
                name=name,
                command=list(command),
                exit_code=-1,
                stdout_tail=stdout_tail,
                stderr_tail=stderr_tail,
                duration_seconds=duration,
                timed_out=True,
            )
        except FileNotFoundError as exc:
            # Command not on PATH — classify as exit code 127 (shell
            # convention) so the caller can distinguish "ran, failed"
            # from "could not run".
            duration = time.monotonic() - start
            return RungExecution(
                name=name,
                command=list(command),
                exit_code=127,
                stdout_tail="",
                stderr_tail=str(exc),
                duration_seconds=duration,
                timed_out=False,
            )


def git_diff(working_tree: Path | str) -> str:
    """Return a unified diff of the uncommitted changes in ``working_tree``.

    Used by the ladder to detect whether a rung actually mutated
    files (the ``fixed`` / ``partial`` outcome signal). Empty output
    means the tree matches HEAD — either the rung did nothing, or
    it reverted a prior rung's edits.

    Returns an empty string when the directory is not a git working
    tree or ``git`` is not on PATH; callers should treat that as
    "no progress detectable" rather than as an error.
    """
    path = Path(working_tree)
    try:
        completed = subprocess.run(  # noqa: S603,S607 - git diff in a known working tree
            ["git", "diff", "--no-color", "--unified=3"],
            cwd=str(path),
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
    return completed.stdout.decode("utf-8", errors="replace")


__all__ = ["FixLadderSandbox", "RungExecution", "git_diff"]
