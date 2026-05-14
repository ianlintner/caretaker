"""``openclaw_http`` backend — calls the in-cluster openclaw server via HTTP.

Sends the PR diff to openclaw's OpenAI-compatible ``/v1/chat/completions``
endpoint (SSE streaming) and parses the ``caretaker-review`` JSON block
from the accumulated response. No subprocess or CLI binary required in
caretaker's pod.

For fix mode (``fix_run``), sends the review summary + inline comments as
a fix prompt and returns the assistant's summary text. The caller
(``auto_fix.dispatch_auto_fix`` or ``dispatch_pre_escalation_attempt``)
handles the commit + push.

Auth: if ``config.api_key`` is non-empty, a ``Authorization: Bearer``
header is sent. For private in-cluster ingress, leave it empty.
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING

import httpx

from caretaker.pr_reviewer.backends._workdir import (
    WorkdirError,
    cleanup_workdir,
    prepare_workdir,
)
from caretaker.pr_reviewer.handoff_reviewer import (
    OPENCLAW_HTTP_REVIEW_MARKER,
    HandoffReviewerSpec,
)
from caretaker.pr_reviewer.inline_reviewer import InlineReviewComment, ReviewResult

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from caretaker.config import OpenclaWHttpConfig

logger = logging.getLogger(__name__)


class OpenclawHttpError(RuntimeError):
    """Raised when the openclaw HTTP call fails (4xx/5xx, timeout, parse error)."""


_RESULT_TEXT_RE = re.compile(r"```caretaker-review\s*\n(?P<json>.+?)\n\s*```", re.DOTALL)

_REVIEW_PROMPT = """\
You are reviewing a pull request. The repo is cloned in the current
working directory. The PR head is checked out as the current branch.

Steps:
  1. Find changed files: ``git diff --name-only origin/HEAD``.
  2. Read each changed file.
  3. Check neighbouring code with Glob/Grep to understand call sites.
  4. Evaluate: correctness, security, API/back-compat, test coverage.

Output ONE message with exactly these two parts:

  1. A short prose summary (2-6 sentences).

  2. The exact marker ``<!-- caretaker:review-result -->`` on its own
     line, then a fenced JSON block tagged ``caretaker-review``:

     ```caretaker-review
     {
       "verdict": "APPROVE" | "COMMENT" | "REQUEST_CHANGES",
       "summary": "1-3 sentence overall assessment",
       "comments": [{"path": "src/foo.py", "line": 42, "body": "..."}],
       "issue_categories": ["lint","format","type","test",
                            "security","correctness","docs","other"]
     }
     ```

Use REQUEST_CHANGES only for blocking issues. Cap inline comments at 8.
"""

_FIX_PROMPT_TEMPLATE = """\
You are addressing review feedback on a pull request. The repo is
cloned and checked out to the PR head branch in the current directory.

Reviewer verdict: REQUEST_CHANGES

Summary:
---
{summary}
---

{comments_section}

Instructions:
  1. Address every issue. Smallest change that fixes the concern.
  2. Write or fix tests if the review flagged them.
  3. Do NOT change behaviour beyond what was requested.
  4. After editing, output one short summary line (no JSON).

If the fix is unsafe or ambiguous, output exactly:
``CARETAKER_FIX_DECLINED:`` followed by a one-sentence reason.
"""

_PRE_ESCALATION_FIX_PROMPT_TEMPLATE = """\
You are making a final automated fix attempt on a pull request.
{attempt_count} previous automated attempts have already failed.
The repo is cloned and checked out to the PR head branch.

Review summary (from last reviewer pass):
---
{summary}
---

{comments_section}

Prior fix attempt errors (most recent last):
---
{prior_errors}
---

Instructions:
  1. Study the prior errors to understand what went wrong before.
  2. Make the minimal correct fix. Do not repeat approaches that failed.
  3. If the problem is genuinely unsolvable automatically, output
     ``CARETAKER_FIX_DECLINED:`` followed by a one-sentence reason.
  4. Otherwise output one short summary of what you changed.
"""


async def _collect_sse_text(aiter_lines: AsyncIterator[str]) -> str:
    """Accumulate ``delta.content`` chunks from an SSE response iterator.

    Handles standard OpenAI-compatible SSE:
      ``data: {"choices":[{"delta":{"content":"chunk"},"index":0}]}``
    Stops at ``data: [DONE]``. Skips blank lines and keep-alive comments.
    """
    chunks: list[str] = []
    async for line in aiter_lines:
        if not line.startswith("data: "):
            continue
        payload = line[6:]
        if payload == "[DONE]":
            break
        try:
            obj = json.loads(payload)
            delta_content = obj["choices"][0]["delta"].get("content", "")
            if delta_content:
                chunks.append(delta_content)
        except (json.JSONDecodeError, KeyError, IndexError):
            # Fallback: some servers emit SSE with single-quoted or non-standard
            # JSON. Try a lightweight regex extraction of the content value.
            m = re.search(r'"content"\s*:\s*(["\'])(?P<val>.*?)(?<!\\)\1', payload)
            if m:
                chunks.append(m.group("val"))
            else:
                logger.debug("openclaw_http: skipping unparseable SSE line: %s", line[:120])
    return "".join(chunks)


async def _invoke_openclaw(
    *,
    base_url: str,
    api_key: str,
    model: str,
    prompt: str,
    system_prompt: str = "",
    timeout_seconds: int = 300,
) -> str:
    """POST to ``/v1/chat/completions`` with SSE streaming; return full text."""
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    body = {
        "model": model,
        "messages": messages,
        "stream": True,
    }

    url = f"{base_url.rstrip('/')}/v1/chat/completions"
    logger.info("openclaw_http: POST %s (model=%s)", url, model)
    try:
        async with (
            httpx.AsyncClient(timeout=float(timeout_seconds)) as client,
            client.stream("POST", url, json=body, headers=headers) as response,
        ):
            if response.status_code >= 400:
                body_text = await response.aread()
                raise OpenclawHttpError(
                    f"openclaw returned HTTP {response.status_code}: {body_text.decode()[:400]}"
                )
            text = await _collect_sse_text(response.aiter_lines())
    except httpx.TimeoutException as exc:
        raise OpenclawHttpError(f"openclaw timed out after {timeout_seconds}s") from exc
    except httpx.ConnectError as exc:
        raise OpenclawHttpError(f"openclaw unreachable at {base_url}: {exc}") from exc
    logger.info("openclaw_http: received %d chars", len(text))
    return text


def _parse_review_payload(text: str) -> tuple[ReviewResult, bool]:
    """Extract a ReviewResult from the accumulated SSE response text.

    Returns ``(result, was_fallback)``.  ``was_fallback=True`` means the
    ``caretaker-review`` JSON block was absent; the result is a
    COMMENT-verdict fallback so the review does not silently vanish.
    """
    m = _RESULT_TEXT_RE.search(text)
    if not m:
        logger.warning(
            "openclaw_http: no caretaker-review block found; using fallback parse. "
            "Raw text (first 300 chars): %s",
            text[:300],
        )
        return (
            ReviewResult(
                summary=(
                    "**Review by openclaw_http** — parse error: "
                    "the response did not contain a ``caretaker-review`` "
                    "JSON block. Raw output below.\n\n" + text[:800]
                ),
                verdict="COMMENT",
                comments=[
                    InlineReviewComment(
                        path="(parse failure)",
                        line=1,
                        body="openclaw_http: failed to parse structured review output",
                    )
                ],
            ),
            True,
        )

    try:
        payload = json.loads(m.group("json"))
    except json.JSONDecodeError as exc:
        logger.warning("openclaw_http: malformed JSON in caretaker-review block: %s", exc)
        return (
            ReviewResult(
                summary="openclaw_http: malformed JSON in review block.", verdict="COMMENT"
            ),
            True,
        )

    if not isinstance(payload, dict):
        return (
            ReviewResult(
                summary="openclaw_http: review payload is not a JSON object.", verdict="COMMENT"
            ),
            True,
        )

    verdict = payload.get("verdict", "COMMENT")
    if verdict not in {"APPROVE", "COMMENT", "REQUEST_CHANGES"}:
        verdict = "COMMENT"

    summary = payload.get("summary", "").strip() if isinstance(payload.get("summary"), str) else ""
    raw_comments = payload.get("comments") or []
    comments: list[InlineReviewComment] = []
    if isinstance(raw_comments, list):
        for entry in raw_comments[:8]:
            if not isinstance(entry, dict):
                continue
            path, line, body = entry.get("path"), entry.get("line"), entry.get("body")
            if (
                isinstance(path, str)
                and path
                and isinstance(line, int)
                and line > 0
                and isinstance(body, str)
                and body.strip()
            ):
                comments.append(InlineReviewComment(path=path, line=line, body=body.strip()))

    _valid_cats = {"lint", "format", "type", "test", "security", "correctness", "docs", "other"}
    raw_cats = payload.get("issue_categories") or []
    issue_categories = [c for c in raw_cats if isinstance(c, str) and c in _valid_cats]

    return (
        ReviewResult(
            summary=(
                "**Review by openclaw_http (in-cluster)**\n\n"
                f"{summary or 'openclaw returned no summary text.'}"
            ),
            verdict=verdict,
            comments=comments,
            issue_categories=issue_categories,
        ),
        False,
    )


async def _prepare_workdir(
    pr_url: str,
    *,
    config: OpenclaWHttpConfig,
    head_branch: str | None = None,
) -> tuple[str, object]:
    """Thin wrapper so workdir prep is mockable in tests."""
    return await prepare_workdir(
        pr_url,
        clone_depth=50,
        workdir_root=None,
        head_branch=head_branch,
    )


async def run(
    *,
    pr_url: str,
    config: OpenclaWHttpConfig,
) -> ReviewResult:
    """Review backend entry-point: clone, call openclaw, parse, return ReviewResult."""
    workdir: str | None = None
    success = False
    try:
        workdir, _parsed = await _prepare_workdir(pr_url, config=config)
        text = await _invoke_openclaw(
            base_url=config.base_url,
            api_key=config.api_key,
            model=config.model,
            prompt=_REVIEW_PROMPT,
            timeout_seconds=config.timeout_seconds,
        )
        result, _fallback = _parse_review_payload(text)
        success = True
        return result
    except OpenclawHttpError:
        raise
    except WorkdirError as exc:
        raise OpenclawHttpError(str(exc)) from exc
    finally:
        if workdir is not None:
            cleanup_workdir(workdir, keep=(not success and config.keep_workdir_on_failure))


def _build_fix_prompt(*, summary: str, comments: list[InlineReviewComment]) -> str:
    if comments:
        rendered = "\n".join(f"- `{c.path}:{c.line}` — {c.body}" for c in comments[:8])
        comments_section = f"Inline comments:\n{rendered}\n"
    else:
        comments_section = "(no inline comments)\n"
    return _FIX_PROMPT_TEMPLATE.format(
        summary=summary.strip() or "(no summary)",
        comments_section=comments_section,
    )


def _build_pre_escalation_fix_prompt(
    *,
    summary: str,
    comments: list[InlineReviewComment],
    prior_errors: str,
    attempt_count: int,
) -> str:
    if comments:
        rendered = "\n".join(f"- `{c.path}:{c.line}` — {c.body}" for c in comments[:8])
        comments_section = f"Inline comments:\n{rendered}\n"
    else:
        comments_section = "(no inline comments)\n"
    return _PRE_ESCALATION_FIX_PROMPT_TEMPLATE.format(
        attempt_count=attempt_count,
        summary=summary.strip() or "(no summary)",
        comments_section=comments_section,
        prior_errors=prior_errors.strip()[:4000] or "(no prior error output captured)",
    )


async def fix_run(
    *,
    workdir: str,
    review_summary: str,
    review_comments: list[InlineReviewComment],
    config: OpenclaWHttpConfig,
    prior_errors: str = "",
    attempt_count: int = 0,
) -> str:
    """Invoke openclaw to address review feedback in an already-prepared workdir.

    When ``prior_errors`` is non-empty (pre-escalation path), uses the
    extended prompt that includes the failure history.  Returns the
    assistant's summary text or raises ``OpenclawHttpError`` on the
    ``CARETAKER_FIX_DECLINED:`` sentinel.
    """
    if prior_errors:
        prompt = _build_pre_escalation_fix_prompt(
            summary=review_summary,
            comments=review_comments,
            prior_errors=prior_errors,
            attempt_count=attempt_count,
        )
    else:
        prompt = _build_fix_prompt(summary=review_summary, comments=review_comments)

    text = await _invoke_openclaw(
        base_url=config.base_url,
        api_key=config.api_key,
        model=config.model,
        prompt=prompt,
        timeout_seconds=config.timeout_seconds,
    )
    stripped = text.strip()
    if stripped.startswith("CARETAKER_FIX_DECLINED:"):
        raise OpenclawHttpError(f"openclaw declined to fix: {stripped}")
    return stripped or "(no summary)"


SPEC = HandoffReviewerSpec(
    backend="openclaw_http",
    marker=OPENCLAW_HTTP_REVIEW_MARKER,
    upstream_action_name="openclaw HTTP (in-cluster service)",
    label_color="1d76db",
    label_description="openclaw_http review (in-cluster REST)",
    invocation="local_subprocess",
    runner=run,
)


__all__ = [
    "SPEC",
    "OpenclawHttpError",
    "_collect_sse_text",
    "_parse_review_payload",
    "fix_run",
    "run",
]
