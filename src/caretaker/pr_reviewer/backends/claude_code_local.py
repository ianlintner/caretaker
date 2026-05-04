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
from typing import TYPE_CHECKING

from caretaker.pr_reviewer.backends._subprocess_streaming import stream_subprocess_output
from caretaker.pr_reviewer.backends._workdir import (
    WorkdirError,
    cleanup_workdir,
    parse_pr_url,
    prepare_workdir,
)
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


# Re-exported for backwards compatibility with callers (and existing
# tests) that previously imported this from this module. New code
# should import from ``backends._workdir`` directly.
def _parse_pr_url(pr_url: str):  # type: ignore[no-untyped-def]
    """Wrapper that translates ``WorkdirError`` → ``ClaudeCodeLocalError``.

    Preserves the original (pre-extraction) exception type so callers
    that catch ``ClaudeCodeLocalError`` keep working after the parse
    helper moved into ``backends._workdir``.
    """
    try:
        return parse_pr_url(pr_url)
    except WorkdirError as exc:
        raise ClaudeCodeLocalError(str(exc)) from exc


async def _prepare_workdir(
    pr_url: str,
    *,
    config: ClaudeCodeLocalBackendConfig,
    head_branch: str | None = None,
) -> tuple[str, object]:
    """Backend-flavoured wrapper around :func:`prepare_workdir`."""
    token = (config.extra_env.get("GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN") or "").strip()
    return await prepare_workdir(
        pr_url,
        clone_depth=config.clone_depth,
        workdir_root=config.clone_workdir_root or None,
        head_branch=head_branch,
        github_token=token or None,
    )


def _cleanup_workdir(repo_dir: str, *, keep: bool) -> None:
    cleanup_workdir(repo_dir, keep=keep)


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
    prompt: str = "",
    permission_mode_override: str | None = None,
    allowed_tools_override: list[str] | None = None,
) -> str:
    """Spawn the claude CLI in ``workdir`` and return its stdout text.

    Uses ``--output-format stream-json`` so we get one JSON event per
    line and can stream them through the logger as they arrive. The
    final ``result`` event contains the assistant's full message text,
    which the parser then walks.

    ``prompt`` defaults to the review prompt; pass ``_FIX_PROMPT_TEMPLATE``-
    interpolated text for fix mode. ``permission_mode_override`` /
    ``allowed_tools_override`` are for fix mode where ``acceptEdits``
    + ``Edit``/``Write`` tools are required for claude to actually
    modify files.
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

    permission_mode = permission_mode_override or config.permission_mode
    allowed_tools = allowed_tools_override or config.allowed_tools

    args = [
        resolved,
        "-p",
        prompt or _REVIEW_PROMPT,
        "--output-format",
        "stream-json",
        "--verbose",  # required when using stream-json output
        "--permission-mode",
        permission_mode,
    ]
    if allowed_tools:
        args += ["--allowed-tools", " ".join(allowed_tools)]

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
        stream_stdout = await _invoke_claude(workdir=workdir, config=config, prompt=_REVIEW_PROMPT)
        text = _extract_assistant_text(stream_stdout)
        result = _parse_review_payload(text)
        success = True
        return result
    except WorkdirError as exc:
        raise ClaudeCodeLocalError(str(exc)) from exc
    finally:
        if workdir is not None:
            _cleanup_workdir(workdir, keep=(not success and config.keep_workdir_on_failure))


# TODO(fix-prompt): Like the review prompt above, the fix prompt is the
# place to encode your repo's expectations for *how* a coding agent
# should address review feedback (style, what's allowed to change vs
# scope-creep limits, whether to add tests). Tune before turning the
# auto-fix loop on for human PRs.
_FIX_PROMPT_TEMPLATE = """\
You are addressing review feedback on a pull request. The repo is
already cloned and checked out to the PR's head branch in your current
working directory; your edits commit directly to that branch.

The reviewer's verdict was REQUEST_CHANGES with the following summary:

---
{summary}
---

{comments_section}

Your job:
  1. Address each issue. Make the smallest change that fixes the
     concern; do not refactor unrelated code.
  2. If a test was missing or broken, write/fix it.
  3. Do NOT change behaviour beyond what the review asked for.
  4. Run any obvious local validation (e.g. ``ruff check``,
     ``ruff format``) before finishing if those tools are configured
     in the repo.
  5. After making changes, output one short summary line describing
     what you changed. Do NOT output any JSON — caretaker handles the
     commit + push.

If you decide the review is incorrect or the change is unsafe to make
automatically, output exactly the line ``CARETAKER_FIX_DECLINED:``
followed by a one-sentence explanation, and make no file changes.
"""


def _build_fix_prompt(*, summary: str, comments: list[InlineReviewComment]) -> str:
    """Render the fix-mode prompt with the reviewer's feedback embedded."""
    if comments:
        rendered = "\n".join(f"- `{c.path}:{c.line}` — {c.body}" for c in comments[:8])
        comments_section = f"Inline comments:\n{rendered}\n"
    else:
        comments_section = "(no inline comments)\n"
    return _FIX_PROMPT_TEMPLATE.format(
        summary=summary.strip() or "(no summary)", comments_section=comments_section
    )


# Tools claude needs to actually edit files in fix mode. Caller can
# narrow further via config.allowed_tools_for_fix (not yet exposed —
# the default below is the minimum needed).
_FIX_ALLOWED_TOOLS = ["Read", "Glob", "Grep", "Bash", "Edit", "Write"]


async def fix_run(
    *,
    workdir: str,
    review_summary: str,
    review_comments: list[InlineReviewComment],
    config: ClaudeCodeLocalBackendConfig,
) -> str:
    """Invoke claude in fix mode against an already-prepared workdir.

    Unlike :func:`run`, this is given an existing workdir (the auto-fix
    dispatcher prepared it with ``head_branch`` so a later push targets
    the right branch). Returns the assistant's final summary text. The
    caller decides whether to commit + push by inspecting ``git diff``
    on the workdir afterward.

    Raises :class:`ClaudeCodeLocalError` on subprocess failure or on the
    sentinel ``CARETAKER_FIX_DECLINED:`` return so the dispatcher can
    log "agent refused" rather than committing a no-op.
    """
    prompt = _build_fix_prompt(summary=review_summary, comments=review_comments)
    stream_stdout = await _invoke_claude(
        workdir=workdir,
        config=config,
        prompt=prompt,
        permission_mode_override="acceptEdits",
        allowed_tools_override=_FIX_ALLOWED_TOOLS,
    )
    text = _extract_assistant_text(stream_stdout).strip()
    if text.startswith("CARETAKER_FIX_DECLINED:"):
        raise ClaudeCodeLocalError(f"claude declined to fix: {text}")
    return text or "(no summary)"


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
    "fix_run",
    "run",
]
