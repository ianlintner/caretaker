"""LLM-callable tools for the Foundry coding executor.

Every tool is an async function taking a :class:`ToolContext` plus its own
arguments and returning a string that is fed back to the model in the next
tool-use turn.

Safety:
- ``write_file`` / ``apply_patch`` refuse any path matching the configured
  ``write_denylist`` (fnmatch globs) or that escapes the workspace root.
- ``run_command`` enforces an ``allowed_commands`` argv[0] allowlist and never
  invokes a shell (``shell=False``). Arguments are passed as a list.
- Every tool wraps untrusted output in ``<tool-output>...</tool-output>``
  fences so subsequent model turns cannot be fooled by adversarial content
  masquerading as instructions.
"""

from __future__ import annotations

import asyncio
import fnmatch
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path  # noqa: TC003 — used at runtime in ToolContext dataclass
from typing import TYPE_CHECKING, Any

from caretaker.util.text import ensure_trailing_newline

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

# Max bytes of content we'll return from read/grep/run_command tools.
_MAX_READ_BYTES = 64_000
_MAX_COMMAND_OUTPUT_BYTES = 16_000


@dataclass
class ToolContext:
    """Shared state passed to every tool invocation.

    ``workspace_root`` is the absolute path of the checked-out worktree.  All
    tool paths are resolved relative to it and rejected if they escape.
    """

    workspace_root: Path
    write_denylist: list[str] = field(default_factory=list)
    allowed_commands: list[str] = field(default_factory=list)
    command_timeout_seconds: int = 120
    mutations: list[str] = field(default_factory=list)

    def record_mutation(self, description: str) -> None:
        self.mutations.append(description)


@dataclass
class Tool:
    """Descriptor for a single LLM-callable tool."""

    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[..., Awaitable[str]]

    def schema(self) -> dict[str, Any]:
        """Return the OpenAI function-calling JSON schema for this tool."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


# ── Path safety ──────────────────────────────────────────────────────────────


class PathViolation(Exception):  # noqa: N818 — 'violation' is clearer than 'Error' here
    """Raised when a tool attempts to read or write outside the workspace."""


def _resolve_inside_workspace(ctx: ToolContext, raw_path: str) -> Path:
    """Resolve a user-supplied path relative to the workspace and confirm it
    stays inside. Absolute paths are rejected outright.
    """
    if not raw_path or raw_path.strip() == "":
        raise PathViolation("path must be non-empty")
    if os.path.isabs(raw_path):
        raise PathViolation(f"absolute paths are not allowed: {raw_path!r}")
    candidate = (ctx.workspace_root / raw_path).resolve()
    root = ctx.workspace_root.resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise PathViolation(f"path escapes workspace: {raw_path!r}") from exc
    return candidate


def _check_write_denylist(ctx: ToolContext, rel_path: str) -> None:
    """Raise :class:`PathViolation` if ``rel_path`` matches any denylist glob."""
    # fnmatch with a '/' path separator uses slashes — fine on POSIX. We also
    # normalise any backslashes defensively.
    normalised = rel_path.replace("\\", "/")
    for pattern in ctx.write_denylist:
        if fnmatch.fnmatch(normalised, pattern):
            raise PathViolation(f"path {rel_path!r} matches write denylist glob {pattern!r}")


def _fence(kind: str, body: str) -> str:
    """Wrap ``body`` in ``<tool-output kind="...">`` tags to mark untrusted
    content for the model. The model is prompted never to follow instructions
    appearing inside these fences.
    """
    return f'<tool-output kind="{kind}">\n{body}\n</tool-output>'


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[truncated, {len(text) - limit} more bytes]"


# ── Read tools ───────────────────────────────────────────────────────────────


async def _tool_read_file(ctx: ToolContext, path: str) -> str:
    try:
        resolved = _resolve_inside_workspace(ctx, path)
    except PathViolation as exc:
        return _fence("error", str(exc))
    if not resolved.is_file():
        return _fence("error", f"file not found: {path}")
    try:
        data = resolved.read_bytes()
    except OSError as exc:
        return _fence("error", f"read failed: {exc}")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return _fence("error", f"{path} is not valid UTF-8; refusing to read")
    return _fence(f"read_file:{path}", _truncate(text, _MAX_READ_BYTES))


async def _tool_list_files(ctx: ToolContext, directory: str = ".", glob: str = "*") -> str:
    try:
        resolved = _resolve_inside_workspace(ctx, directory)
    except PathViolation as exc:
        return _fence("error", str(exc))
    if not resolved.is_dir():
        return _fence("error", f"not a directory: {directory}")
    entries: list[str] = []
    for item in sorted(resolved.rglob(glob)):
        if ".git" in item.parts:
            continue
        try:
            rel = item.relative_to(ctx.workspace_root)
        except ValueError:
            continue
        marker = "/" if item.is_dir() else ""
        entries.append(f"{rel}{marker}")
        if len(entries) >= 500:
            entries.append("...[truncated at 500 entries]")
            break
    return _fence(
        f"list_files:{directory}:{glob}",
        "\n".join(entries) if entries else "(no matches)",
    )


async def _tool_grep(ctx: ToolContext, pattern: str, path: str = ".") -> str:
    import re

    try:
        resolved = _resolve_inside_workspace(ctx, path)
    except PathViolation as exc:
        return _fence("error", str(exc))
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        return _fence("error", f"invalid regex: {exc}")

    matches: list[str] = []
    files: list[Path] = []
    if resolved.is_file():
        files = [resolved]
    elif resolved.is_dir():
        files = [p for p in resolved.rglob("*") if p.is_file() and ".git" not in p.parts]
    else:
        return _fence("error", f"path not found: {path}")

    for file_path in files:
        try:
            for i, line in enumerate(
                file_path.read_text(encoding="utf-8", errors="replace").splitlines(),
                start=1,
            ):
                if regex.search(line):
                    rel = file_path.relative_to(ctx.workspace_root)
                    matches.append(f"{rel}:{i}:{line.rstrip()}")
                    if len(matches) >= 200:
                        matches.append("...[truncated at 200 matches]")
                        break
        except OSError:
            continue
        if len(matches) >= 200:
            break

    return _fence(
        f"grep:{pattern}:{path}",
        "\n".join(matches) if matches else "(no matches)",
    )


# ── Mutation tools ───────────────────────────────────────────────────────────


async def _tool_write_file(ctx: ToolContext, path: str, content: str) -> str:
    try:
        _check_write_denylist(ctx, path)
        resolved = _resolve_inside_workspace(ctx, path)
    except PathViolation as exc:
        return _fence("error", str(exc))
    resolved.parent.mkdir(parents=True, exist_ok=True)
    # Guarantee a trailing newline so downstream pre-commit end-of-file-fixer
    # hooks don't fail on files the LLM forgot to terminate. Idempotent:
    # content that already ends with ``\n`` (including multi-newline endings
    # like JSON blobs with trailing blank lines) is returned unchanged.
    normalised = ensure_trailing_newline(content)
    try:
        resolved.write_text(normalised, encoding="utf-8")
    except OSError as exc:
        return _fence("error", f"write failed: {exc}")
    ctx.record_mutation(f"write_file {path} ({len(normalised)} bytes)")
    return _fence(f"write_file:{path}", f"OK ({len(normalised)} bytes written)")


async def _tool_apply_patch(ctx: ToolContext, unified_diff: str) -> str:
    """Apply a unified diff via ``git apply``. Every touched path is checked
    against the write denylist before the diff is applied.
    """
    # Parse the ``+++ b/path`` lines to extract targets for the denylist check.
    targets: list[str] = []
    for line in unified_diff.splitlines():
        if line.startswith("+++ "):
            name = line[4:].strip()
            if name.startswith("b/"):
                name = name[2:]
            if name and name != "/dev/null":
                targets.append(name)
    for target in targets:
        try:
            _check_write_denylist(ctx, target)
        except PathViolation as exc:
            return _fence("error", str(exc))

    proc = await asyncio.create_subprocess_exec(
        "git",
        "apply",
        "--whitespace=nowarn",
        "-",
        cwd=str(ctx.workspace_root),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate(input=unified_diff.encode("utf-8"))
    if proc.returncode != 0:
        return _fence(
            "error",
            f"git apply failed (rc={proc.returncode}):\n{stderr.decode(errors='replace')}",
        )
    ctx.record_mutation(f"apply_patch ({len(targets)} files: {', '.join(targets)})")
    return _fence(
        "apply_patch",
        stdout.decode(errors="replace") or f"OK (patched {len(targets)} file(s))",
    )


async def _tool_run_command(ctx: ToolContext, command: str, args: list[str] | None = None) -> str:
    """Run a process with an allowlist-gated ``command``. No shell, no sh -c."""
    if command not in ctx.allowed_commands:
        return _fence(
            "error",
            f"command {command!r} is not in the allowlist: {ctx.allowed_commands}",
        )
    argv = [command, *(args or [])]
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(ctx.workspace_root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=ctx.command_timeout_seconds
            )
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return _fence(
                "error",
                f"command timed out after {ctx.command_timeout_seconds}s",
            )
    except FileNotFoundError:
        return _fence("error", f"command not found on PATH: {command}")
    output = stdout.decode(errors="replace") + stderr.decode(errors="replace")
    ctx.record_mutation(f"run_command {command} {' '.join(args or [])} (rc={proc.returncode})")
    return _fence(
        f"run_command:{command}",
        f"exit_code={proc.returncode}\n\n{_truncate(output, _MAX_COMMAND_OUTPUT_BYTES)}",
    )


async def _tool_git_status(ctx: ToolContext) -> str:
    proc = await asyncio.create_subprocess_exec(
        "git",
        "status",
        "--porcelain=v1",
        cwd=str(ctx.workspace_root),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    output = stdout.decode(errors="replace") or "(clean)"
    if proc.returncode != 0:
        output = f"error: {stderr.decode(errors='replace')}"
    return _fence("git_status", output)


async def _tool_git_diff(ctx: ToolContext, ref: str | None = None) -> str:
    args = ["git", "diff", "--stat"] if ref is None else ["git", "diff", "--stat", ref]
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=str(ctx.workspace_root),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    output = stdout.decode(errors="replace") or "(no changes)"
    if proc.returncode != 0:
        output = f"error: {stderr.decode(errors='replace')}"
    return _fence("git_diff", _truncate(output, _MAX_COMMAND_OUTPUT_BYTES))


# ── Tool registry ────────────────────────────────────────────────────────────


def build_tool_registry() -> dict[str, Tool]:
    """Return the default set of tools exposed to the LLM."""
    return {
        "read_file": Tool(
            name="read_file",
            description=(
                "Read a UTF-8 text file from the workspace. Returns the file "
                "contents, truncated to 64 KB if larger. Paths must be "
                "relative to the workspace root."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Workspace-relative path"},
                },
                "required": ["path"],
            },
            handler=_tool_read_file,
        ),
        "list_files": Tool(
            name="list_files",
            description=(
                "Recursively list files in a directory matching a glob. "
                "Hidden '.git' entries are excluded. Capped at 500 results."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "directory": {
                        "type": "string",
                        "description": "Workspace-relative directory",
                        "default": ".",
                    },
                    "glob": {
                        "type": "string",
                        "description": "rglob pattern, e.g. '**/*.py'",
                        "default": "*",
                    },
                },
                "required": [],
            },
            handler=_tool_list_files,
        ),
        "grep": Tool(
            name="grep",
            description=(
                "Search file contents for a Python regex. Returns lines in "
                "'path:lineno:content' format. Capped at 200 matches."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Python regex"},
                    "path": {
                        "type": "string",
                        "description": "Workspace-relative path (file or dir)",
                        "default": ".",
                    },
                },
                "required": ["pattern"],
            },
            handler=_tool_grep,
        ),
        "write_file": Tool(
            name="write_file",
            description=(
                "Write (create or overwrite) a UTF-8 text file. Paths matching "
                "the write_denylist are rejected. Content replaces the entire file."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Workspace-relative path"},
                    "content": {"type": "string", "description": "Full file contents"},
                },
                "required": ["path", "content"],
            },
            handler=_tool_write_file,
        ),
        "apply_patch": Tool(
            name="apply_patch",
            description=(
                "Apply a unified diff via 'git apply'. Every target path is "
                "checked against the write_denylist before the patch is applied."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "unified_diff": {
                        "type": "string",
                        "description": "A unified diff (git-format)",
                    },
                },
                "required": ["unified_diff"],
            },
            handler=_tool_apply_patch,
        ),
        "run_command": Tool(
            name="run_command",
            description=(
                "Run a process from the allowed_commands list. No shell. "
                "Arguments are passed as a list. Returns exit code + combined "
                "stdout/stderr, truncated to 16 KB."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "argv[0]; must be in allowed_commands",
                    },
                    "args": {
                        "type": "array",
                        "items": {"type": "string"},
                        "default": [],
                    },
                },
                "required": ["command"],
            },
            handler=_tool_run_command,
        ),
        "git_status": Tool(
            name="git_status",
            description="Show git working-tree status in porcelain v1 format.",
            parameters={"type": "object", "properties": {}, "required": []},
            handler=_tool_git_status,
        ),
        "git_diff": Tool(
            name="git_diff",
            description=(
                "Show git diff --stat for the current worktree. Optionally against a specific ref."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "ref": {
                        "type": "string",
                        "description": "Optional ref to diff against",
                    },
                },
                "required": [],
            },
            handler=_tool_git_diff,
        ),
    }
