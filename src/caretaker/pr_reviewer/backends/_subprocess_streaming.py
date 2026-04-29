"""Shared async subprocess streaming helper for local-subprocess backends.

Backends that shell out to long-running CLIs (pr-agent,
claude_code_local, greptile-when-implemented) use this to pipe each
subprocess line through the module logger as it arrives, so progress is
visible in caretaker's job log (e.g. GitHub Actions runner) while the
subprocess is still running instead of appearing in one batch after exit.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable


async def stream_lines(
    stream: asyncio.StreamReader | None,
    *,
    log: Callable[[str], None],
) -> str:
    """Drain ``stream`` line-by-line, log each line, return the full text."""
    if stream is None:
        return ""
    chunks: list[str] = []
    while True:
        line_bytes = await stream.readline()
        if not line_bytes:
            break
        line = line_bytes.decode("utf-8", errors="replace")
        chunks.append(line)
        log(line.rstrip("\n"))
    return "".join(chunks)


async def stream_subprocess_output(
    proc: asyncio.subprocess.Process,
    *,
    timeout_seconds: int,
    stdout_log: Callable[[str], None],
    stderr_log: Callable[[str], None],
) -> tuple[str, str]:
    """Stream a subprocess's stdout + stderr concurrently, with timeout.

    On timeout the process is killed, the stream tasks are cancelled,
    and ``TimeoutError`` is re-raised so the caller can wrap it in a
    backend-specific exception type. Returns ``(stdout, stderr)`` text
    on clean exit; the caller still needs to inspect ``proc.returncode``.
    """
    stdout_task = asyncio.create_task(stream_lines(proc.stdout, log=stdout_log))
    stderr_task = asyncio.create_task(stream_lines(proc.stderr, log=stderr_log))
    try:
        stdout, stderr = await asyncio.wait_for(
            asyncio.gather(stdout_task, stderr_task), timeout=timeout_seconds
        )
        await proc.wait()
        return stdout, stderr
    except TimeoutError:
        proc.kill()
        for task in (stdout_task, stderr_task):
            task.cancel()
        with contextlib.suppress(Exception):
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
        with contextlib.suppress(Exception):
            await proc.wait()
        raise


__all__ = ["stream_lines", "stream_subprocess_output"]
