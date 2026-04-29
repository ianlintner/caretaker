"""pr-agent (https://github.com/The-PR-Agent/pr-agent) CLI backend.

Caretaker shells out to the pr-agent CLI as a separate subprocess and
transforms the markdown output into a :class:`ReviewResult` that
``post_review`` can submit through the GitHub Reviews API. Keeping the
boundary at "subprocess invocation" — never importing pr-agent code
into the caretaker process — preserves the AGPL-3.0 aggregation
boundary so caretaker itself stays under its own licence.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
from dataclasses import dataclass
from typing import TYPE_CHECKING

from caretaker.pr_reviewer.backends._subprocess_streaming import stream_subprocess_output
from caretaker.pr_reviewer.handoff_reviewer import (
    PR_AGENT_REVIEW_MARKER,
    HandoffReviewerSpec,
)
from caretaker.pr_reviewer.inline_reviewer import InlineReviewComment, ReviewResult

if TYPE_CHECKING:
    from caretaker.config import PRAgentBackendConfig

logger = logging.getLogger(__name__)


class PRAgentInvocationError(RuntimeError):
    """Raised when the pr-agent CLI fails (non-zero exit, timeout, missing binary)."""


@dataclass(frozen=True)
class PRAgentRawResult:
    """Captured stdout/stderr and exit code from one pr-agent invocation."""

    stdout: str
    stderr: str
    returncode: int


async def run_pr_agent(
    *,
    pr_url: str,
    cli_path: str = "pr-agent",
    command: str = "review",
    timeout_seconds: int = 180,
    extra_env: dict[str, str] | None = None,
) -> PRAgentRawResult:
    """Invoke ``<cli_path> --pr_url <pr_url> <command>`` as a subprocess.

    Streams the subprocess's stdout/stderr line-by-line through the
    module logger so live progress is visible in caretaker's job log
    (e.g. GitHub Actions runner output) while the review is in flight,
    then returns the accumulated output for downstream parsing.

    Raises :class:`PRAgentInvocationError` on missing binary, timeout, or
    non-zero exit so the caller can log and fall back. The configured
    ``extra_env`` is layered onto the inherited environment — caretaker
    does not strip anything else, so a deployment that relies on a
    process-wide ``OPENAI_KEY`` keeps working without ceremony.
    """
    resolved = shutil.which(cli_path) or cli_path
    if not os.path.isabs(resolved) and not shutil.which(resolved):
        raise PRAgentInvocationError(
            f"pr-agent CLI not found at {cli_path!r}; install it (`pip install pr-agent`) "
            "or configure `pr_reviewer.pr_agent.cli_path` to a pinned absolute path"
        )

    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)

    logger.info("pr-agent: invoking %s %s for %s", resolved, command, pr_url)
    try:
        proc = await asyncio.create_subprocess_exec(
            resolved,
            "--pr_url",
            pr_url,
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
    except FileNotFoundError as exc:
        raise PRAgentInvocationError(f"pr-agent CLI not executable: {exc}") from exc

    try:
        stdout, stderr = await stream_subprocess_output(
            proc,
            timeout_seconds=timeout_seconds,
            stdout_log=lambda line: logger.info("pr-agent | %s", line),
            stderr_log=lambda line: logger.warning("pr-agent! %s", line),
        )
    except TimeoutError as exc:
        raise PRAgentInvocationError(
            f"pr-agent timed out after {timeout_seconds}s on {pr_url}"
        ) from exc

    if proc.returncode != 0:
        raise PRAgentInvocationError(
            f"pr-agent exited {proc.returncode} on {pr_url}: "
            f"{stderr.strip() or stdout.strip()[:500]}"
        )
    return PRAgentRawResult(stdout=stdout, stderr=stderr, returncode=proc.returncode)


# pr-agent's `/review` output uses Markdown sections like:
#   ## PR Reviewer Guide 🔍
#   ⏱️ Estimated effort to review [1-5]: 3
#   🧪 No relevant tests
#   🔒 Security concerns: ...
#   ⚡ Recommended focus areas for review
#   ## PR Code Suggestions ✨
#   relevant_file | suggestion |
# We extract a conservative ``ReviewResult`` from these — a 1-3 sentence
# summary, a verdict (default COMMENT, REQUEST_CHANGES on explicit
# security/critical signals), and inline comments parsed from the
# suggestions table when present.
# Negative lookbehind on ``no `` / ``no_`` so phrasing like "no security
# concerns found" doesn't trip the security check (caught by Claude Code's
# review of the PR that introduced this file).
_SECURITY_RE = re.compile(
    r"(?<!\bno\s)(security concern|critical issue|🚨|severe|exploit)",
    re.IGNORECASE,
)
_NO_BLOCKERS_RE = re.compile(
    r"(no security concerns|no relevant tests|looks good|lgtm)", re.IGNORECASE
)
# Loose suggestions table parser. pr-agent emits one row per suggestion
# with columns ``relevant file`` / ``suggestion``. We don't try to parse
# specific line numbers because pr-agent's table format varies; without
# a reliable line, the comment goes into the summary instead of inline.
_TABLE_ROW_RE = re.compile(
    r"^\|\s*(?P<file>[^|]+?)\s*\|\s*(?P<suggestion>.+?)\s*\|\s*$",
    re.MULTILINE,
)


def _derive_verdict(stdout: str) -> str:
    if _SECURITY_RE.search(stdout):
        return "REQUEST_CHANGES"
    return "COMMENT"


def _derive_summary(stdout: str) -> str:
    # First non-empty paragraph that isn't a heading or table marker.
    for chunk in stdout.split("\n\n"):
        text = chunk.strip()
        if not text or text.startswith(("#", "|", "---", "```")):
            continue
        # Cap at ~600 chars so the GitHub Reviews body stays scannable.
        return text[:600]
    return "pr-agent produced no narrative summary."


def _derive_comments(stdout: str, *, max_comments: int = 8) -> list[InlineReviewComment]:
    comments: list[InlineReviewComment] = []
    for match in _TABLE_ROW_RE.finditer(stdout):
        file_path = match.group("file").strip()
        suggestion = match.group("suggestion").strip()
        # Skip header rows / divider rows / empty cells.
        if not file_path or file_path.lower() in {"relevant file", "file", "---"}:
            continue
        if not suggestion or suggestion.lower() in {"suggestion", "---"}:
            continue
        # Without a reliable line number, anchor at line 1 and let the
        # reviewer click through. The comment body cites the file so
        # context is preserved even when the anchor isn't precise.
        comments.append(
            InlineReviewComment(
                path=file_path,
                line=1,
                body=f"_pr-agent suggestion:_ {suggestion[:280]}",
            )
        )
        if len(comments) >= max_comments:
            break
    return comments


def to_caretaker_review(raw: PRAgentRawResult, *, include_full_output: bool = True) -> ReviewResult:
    """Transform pr-agent's stdout into the shared ``ReviewResult`` shape."""
    summary = _derive_summary(raw.stdout)
    verdict = _derive_verdict(raw.stdout)
    comments = _derive_comments(raw.stdout)

    body_parts = [
        "**Review by [pr-agent](https://github.com/The-PR-Agent/pr-agent) "
        "(invoked by caretaker as a third-party reviewer)**",
        "",
        summary,
    ]
    if include_full_output:
        body_parts.extend(
            [
                "",
                "<details><summary>Full pr-agent output</summary>",
                "",
                raw.stdout.strip()[:8000],
                "",
                "</details>",
            ]
        )
    if _NO_BLOCKERS_RE.search(raw.stdout) and verdict == "COMMENT":
        body_parts.extend(["", "_pr-agent did not flag blocking issues._"])

    return ReviewResult(
        summary="\n".join(body_parts),
        verdict=verdict,
        comments=comments,
    )


async def run(
    *,
    pr_url: str,
    config: PRAgentBackendConfig,
) -> ReviewResult:
    """High-level entry point used by the spec runner.

    Wraps :func:`run_pr_agent` and :func:`to_caretaker_review` into one
    coroutine so callers don't need to know about the intermediate
    ``PRAgentRawResult`` shape.
    """
    raw = await run_pr_agent(
        pr_url=pr_url,
        cli_path=config.cli_path,
        command=config.command,
        timeout_seconds=config.timeout_seconds,
        extra_env=dict(config.extra_env) if config.extra_env else None,
    )
    return to_caretaker_review(raw, include_full_output=True)


SPEC = HandoffReviewerSpec(
    backend="pr_agent",
    marker=PR_AGENT_REVIEW_MARKER,
    upstream_action_name="pr-agent CLI (local subprocess)",
    label_color="2ea44f",
    label_description="pr-agent review (pluggable backend)",
    invocation="local_subprocess",
    runner=run,
)


__all__ = [
    "PRAgentInvocationError",
    "PRAgentRawResult",
    "SPEC",
    "run",
    "run_pr_agent",
    "to_caretaker_review",
]
