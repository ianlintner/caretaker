"""``claude_code_local`` backend — runs the Claude Code CLI in caretaker's pod.

An alternative to the ``claude_code`` backend (which triggers
``anthropics/claude-code-action`` in the target repo via a mention
comment). This backend instead:

  1. Clones the PR's head into a temp workdir inside caretaker's pod
  2. Spawns ``claude -p "<review prompt>" --output-format=stream-json``
  3. Streams every event to caretaker's logger (visible live in GH
     Actions / kubectl logs)
  4. Parses the final ``result`` event's text for a ``caretaker-review``
     JSON block, transforms it into a :class:`ReviewResult`
  5. Cleans up the workdir (kept on disk only when the run failed AND
     the operator opted into ``keep_workdir_on_failure``)

Compared to the action-based path it: removes the per-target-repo
workflow install requirement, centralises credentials and observability
in caretaker, and gives synchronous logs. Compared to ``inline_reviewer``
(direct LLM API call) it: gives Claude tool access (Read/Glob/Grep/Bash)
so the review can navigate the tree, not just consume the diff.

For multi-tenant or high-PR-rate fleets, swap the in-pod subprocess for
a Kubernetes Job per review so each session has its own resource
budget. The runner shape (:func:`run`) is the same; only
:func:`_invoke_claude` would change. See the TODO at the bottom of this
file for the path.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import tempfile
import urllib.parse
from dataclasses import dataclass
from typing import TYPE_CHECKING

from caretaker.pr_reviewer.backends._subprocess_streaming import stream_subprocess_output
from caretaker.pr_reviewer.handoff_reviewer import (
    CLAUDE_CODE_LOCAL_REVIEW_MARKER,
    HandoffReviewerSpec,
)
from caretaker.pr_reviewer.inline_reviewer import InlineReviewComment, ReviewResult

if TYPE_CHECKING:
    from caretaker.config import ClaudeCodeLocalBackendConfig

logger = logging.getLogger(__name__)


class ClaudeCodeLocalError(RuntimeError):
    """Raised when the local claude CLI run fails (clone, invocation, parse)."""


@dataclass(frozen=True)
class _ParsedPRURL:
    owner: str
    repo: str
    number: int


def _parse_pr_url(pr_url: str) -> _ParsedPRURL:
    """Extract owner/repo/PR number from a github PR URL.

    Accepts both ``https://github.com/owner/repo/pull/N`` (browser URL)
    and ``https://api.github.com/repos/owner/repo/pulls/N`` (API URL)
    so the caller doesn't need to normalise.
    """
    parsed = urllib.parse.urlparse(pr_url)
    parts = [p for p in parsed.path.split("/") if p]
    # Browser form: owner/repo/pull/N
    if len(parts) >= 4 and parts[-2] in {"pull", "pulls"}:
        return _ParsedPRURL(owner=parts[-4], repo=parts[-3], number=int(parts[-1]))
    # API form: repos/owner/repo/pulls/N
    if len(parts) >= 5 and parts[0] == "repos" and parts[-2] in {"pull", "pulls"}:
        return _ParsedPRURL(owner=parts[1], repo=parts[2], number=int(parts[-1]))
    raise ClaudeCodeLocalError(f"cannot parse PR URL: {pr_url!r}")


def _clone_url(parsed: _ParsedPRURL, *, github_token: str | None) -> str:
    """Build the HTTPS clone URL, embedding the token when present.

    Token-embedded clone is the standard pattern for GitHub Actions
    runners; the token never lands on disk because git only uses it for
    the HTTP exchange. If no token is configured, fall back to the
    public URL — works for public repos, fails clearly for private.
    """
    if github_token:
        return f"https://x-access-token:{github_token}@github.com/{parsed.owner}/{parsed.repo}.git"
    return f"https://github.com/{parsed.owner}/{parsed.repo}.git"


async def _run_git(
    *args: str, cwd: str | None = None, timeout: int = 120, env: dict[str, str] | None = None
) -> str:
    """Run ``git <args>``, stream output, raise ``ClaudeCodeLocalError`` on failure."""
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
        raise ClaudeCodeLocalError(f"git {args[0]} timed out after {timeout}s") from exc
    if proc.returncode != 0:
        raise ClaudeCodeLocalError(
            f"git {' '.join(args)} exited {proc.returncode}: "
            f"{stderr.strip() or stdout.strip()[:500]}"
        )
    return stdout


async def _prepare_workdir(
    pr_url: str,
    *,
    config: ClaudeCodeLocalBackendConfig,
) -> tuple[str, _ParsedPRURL]:
    """Clone the repo + check out the PR head into a fresh temp dir.

    Returns ``(workdir_path, parsed_url)``. Caller is responsible for
    cleanup via :func:`_cleanup_workdir`.
    """
    parsed = _parse_pr_url(pr_url)
    root = config.clone_workdir_root or None
    workdir = tempfile.mkdtemp(prefix=f"caretaker-claude-{parsed.repo}-{parsed.number}-", dir=root)
    logger.info(
        "claude_code_local: workdir=%s for %s/%s#%d",
        workdir,
        parsed.owner,
        parsed.repo,
        parsed.number,
    )

    token = (config.extra_env.get("GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN") or "").strip()
    clone_url = _clone_url(parsed, github_token=token)
    # Clone shallow into an inner ``repo/`` so claude's cwd is the repo
    # itself, not a wrapper dir. Use ``--quiet`` so we don't double-log
    # progress (we already stream stderr).
    repo_dir = os.path.join(workdir, "repo")
    await _run_git(
        "clone",
        "--depth",
        str(config.clone_depth),
        "--quiet",
        clone_url,
        repo_dir,
    )
    # Fetch the PR head ref into a local branch and check it out.
    pr_ref = f"refs/pull/{parsed.number}/head"
    await _run_git("fetch", "origin", f"{pr_ref}:caretaker/pr-head", cwd=repo_dir)
    await _run_git("checkout", "caretaker/pr-head", cwd=repo_dir)
    return repo_dir, parsed


def _cleanup_workdir(workdir: str, *, keep: bool) -> None:
    """Remove the workdir unless the operator asked to keep it for debugging."""
    if keep:
        logger.warning("claude_code_local: keeping workdir %s for inspection", workdir)
        return
    parent = os.path.dirname(workdir.rstrip("/"))
    shutil.rmtree(parent, ignore_errors=True)


# TODO(reviewer-prompt): The prompt below is the highest-leverage knob
# in this whole backend — it shapes what claude looks at, what verdict
# thresholds it applies, and how strict the JSON output is. Tune it to
# your repo's review priorities (security weight, test coverage gates,
# style preferences, anything domain-specific) before turning this
# backend on in production. The output schema MUST stay stable so the
# parser keeps working — keep the ``caretaker-review`` fence + JSON
# field names exactly as documented.
_REVIEW_PROMPT = """\
You are reviewing a pull request that has been freshly cloned into the
current working directory. The PR head is checked out as the current
branch (``caretaker/pr-head``). The default base branch is the remote
default (run ``git remote show origin`` to confirm).

Steps:
  1. Identify the changed files via ``git diff --name-only origin/HEAD``.
  2. Read each changed file (use the Read tool, not just diff context).
  3. Walk neighbouring code with Glob/Grep to understand call sites.
  4. Evaluate: correctness, security, API/back-compat, test coverage.

Output ONE message with ONLY the following two parts, in order:

  1. A short prose summary of your findings (2–6 sentences).

  2. The exact marker ``<!-- caretaker:review-result -->`` on its own
     line, followed by a fenced JSON block tagged ``caretaker-review``
     with this schema (no comments, strict JSON):

     ```caretaker-review
     {
       "verdict": "APPROVE" | "COMMENT" | "REQUEST_CHANGES",
       "summary": "1–3 sentence overall assessment",
       "comments": [
         {"path": "src/foo.py", "line": 42, "body": "..."}
       ]
     }
     ```

Pick ``REQUEST_CHANGES`` only for blocking issues (security,
correctness, broken tests). ``COMMENT`` for non-blocking observations.
``APPROVE`` only when you have no concerns at all. Cap inline
``comments`` at 8 entries; line numbers refer to the new file
(right-hand side of the diff).
"""


async def _invoke_claude(
    *,
    workdir: str,
    config: ClaudeCodeLocalBackendConfig,
) -> str:
    """Spawn the claude CLI in ``workdir`` and return its stdout text.

    Uses ``--output-format stream-json`` so we get one JSON event per
    line and can stream them through the logger as they arrive. The
    final ``result`` event contains the assistant's full message text,
    which the parser then walks.
    """
    resolved = shutil.which(config.cli_path) or config.cli_path
    if not os.path.isabs(resolved) and not shutil.which(resolved):
        raise ClaudeCodeLocalError(
            f"claude CLI not found at {config.cli_path!r}; install it "
            "(`npm install -g @anthropic-ai/claude-code` or pin a path)"
        )
    env = os.environ.copy()
    if config.extra_env:
        env.update(config.extra_env)

    args = [
        resolved,
        "-p",
        _REVIEW_PROMPT,
        "--output-format",
        "stream-json",
        "--verbose",  # required when using stream-json output
        "--permission-mode",
        config.permission_mode,
    ]
    if config.allowed_tools:
        args += ["--allowed-tools", " ".join(config.allowed_tools)]

    logger.info("claude_code_local: invoking %s in %s", resolved, workdir)
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=workdir,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        stdout, stderr = await stream_subprocess_output(
            proc,
            timeout_seconds=config.timeout_seconds,
            stdout_log=lambda line: logger.info("claude | %s", _truncate(line, 400)),
            stderr_log=lambda line: logger.warning("claude! %s", line),
        )
    except TimeoutError as exc:
        raise ClaudeCodeLocalError(f"claude timed out after {config.timeout_seconds}s") from exc
    if proc.returncode != 0:
        raise ClaudeCodeLocalError(
            f"claude exited {proc.returncode}: {stderr.strip() or stdout.strip()[:500]}"
        )
    return stdout


def _truncate(line: str, max_len: int) -> str:
    """Cap a log line so a streamed JSON event doesn't fill the runner buffer."""
    if len(line) <= max_len:
        return line
    return line[:max_len] + f"… ({len(line) - max_len} more chars)"


_RESULT_TEXT_RE = re.compile(r"```caretaker-review\s*\n(?P<json>.+?)\n\s*```", re.DOTALL)


def _extract_assistant_text(stream_json_stdout: str) -> str:
    """Pull the final assistant text from claude's stream-json output.

    Each non-empty line in ``stream-json`` mode is a JSON event. The
    last event with ``type == "result"`` contains a ``result`` field
    holding the assistant's full final message (or, when the session
    ends without a result, we concatenate ``assistant`` events as a
    fallback so a partial response is still parseable).
    """
    final_result: str | None = None
    assistant_chunks: list[str] = []
    for raw_line in stream_json_stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        etype = event.get("type")
        if etype == "result":
            text = event.get("result")
            if isinstance(text, str):
                final_result = text
        elif etype == "assistant":
            # ``assistant`` events carry an ``message.content`` array of
            # blocks; the text-bearing ones have ``type=text``.
            message = event.get("message") or {}
            for block in message.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "text":
                    text = block.get("text")
                    if isinstance(text, str):
                        assistant_chunks.append(text)
    return final_result if final_result is not None else "\n".join(assistant_chunks)


def _parse_review_payload(assistant_text: str) -> ReviewResult:
    """Find the ``caretaker-review`` JSON block; fall back to a generic COMMENT review.

    The fallback ensures a noisy or non-conforming claude reply still
    produces *some* review on the PR rather than dropping the work.
    """
    match = _RESULT_TEXT_RE.search(assistant_text)
    if not match:
        logger.warning(
            "claude_code_local: no caretaker-review JSON block in claude reply; "
            "wrapping the prose as a COMMENT review (length=%d)",
            len(assistant_text),
        )
        return ReviewResult(
            summary=(
                "**Review by claude_code_local (fallback parse)**\n\n"
                "Claude did not emit a structured `caretaker-review` JSON block; "
                "its prose response is included below.\n\n"
                f"{assistant_text.strip()[:4000]}"
            ),
            verdict="COMMENT",
            comments=[],
        )
    raw = match.group("json").strip()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ClaudeCodeLocalError(f"caretaker-review block is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ClaudeCodeLocalError("caretaker-review payload is not a JSON object")

    summary = payload.get("summary", "").strip() if isinstance(payload.get("summary"), str) else ""
    verdict = payload.get("verdict", "COMMENT")
    if verdict not in {"APPROVE", "COMMENT", "REQUEST_CHANGES"}:
        verdict = "COMMENT"
    raw_comments = payload.get("comments") or []
    comments: list[InlineReviewComment] = []
    if isinstance(raw_comments, list):
        for entry in raw_comments[:8]:
            if not isinstance(entry, dict):
                continue
            path = entry.get("path")
            line = entry.get("line")
            body = entry.get("body")
            if (
                isinstance(path, str)
                and path
                and isinstance(line, int)
                and line > 0
                and isinstance(body, str)
                and body.strip()
            ):
                comments.append(InlineReviewComment(path=path, line=line, body=body.strip()))

    body = (
        "**Review by claude_code_local "
        "(Claude Code CLI in caretaker's pod)**\n\n"
        f"{summary or 'Claude returned no summary text.'}"
    )
    return ReviewResult(summary=body, verdict=verdict, comments=comments)


async def run(
    *,
    pr_url: str,
    config: ClaudeCodeLocalBackendConfig,
) -> ReviewResult:
    """Backend runner — clone, invoke claude, parse, return ``ReviewResult``.

    Workdir cleanup is in a finally so a partial run still tidies up
    unless ``keep_workdir_on_failure`` is set.
    """
    workdir: str | None = None
    success = False
    try:
        workdir, _parsed = await _prepare_workdir(pr_url, config=config)
        stream_stdout = await _invoke_claude(workdir=workdir, config=config)
        text = _extract_assistant_text(stream_stdout)
        result = _parse_review_payload(text)
        success = True
        return result
    finally:
        if workdir is not None:
            _cleanup_workdir(workdir, keep=(not success and config.keep_workdir_on_failure))


# TODO(k8s-job-mode): When deploying to a fleet that runs many concurrent
# reviews, swap the in-pod subprocess for a Kubernetes Job. Suggested
# shape: add ``invocation_mode: Literal["subprocess", "k8s_job"]`` to
# ClaudeCodeLocalBackendConfig and route through a separate
# ``_invoke_claude_via_k8s_job`` helper that templates a Job manifest
# (image, resource limits, secrets, the same prompt + git steps as an
# init container) and tails ``kubectl logs -f`` for the streaming view.
# The :func:`run` body stays the same; only the invocation indirection
# changes. The dispatcher in agent.py already calls ``spec.runner``
# blindly, so no plumbing change above this layer.


SPEC = HandoffReviewerSpec(
    backend="claude_code_local",
    marker=CLAUDE_CODE_LOCAL_REVIEW_MARKER,
    upstream_action_name="Claude Code CLI (in caretaker pod)",
    label_color="6f42c1",
    label_description="claude_code_local review (pluggable backend)",
    invocation="local_subprocess",
    runner=run,
)


__all__ = [
    "SPEC",
    "ClaudeCodeLocalError",
    "run",
]
