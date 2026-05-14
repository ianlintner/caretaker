# openclaw HTTP Integration — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Integrate the in-cluster openclaw server as a PR review backend, auto-fix backend, and pre-escalation last-resort before human digest.

**Architecture:** openclaw's OpenAI-compatible `/v1/chat/completions` SSE endpoint is wrapped in a new `openclaw_http` backend (mirrors `opencode_local` but uses `httpx` instead of subprocess). The pre-escalation rung fires in `auto_fix.py` when `decide_auto_fix` returns `max_attempts_reached` and `auto_fix.pre_escalation_agent = "openclaw_http"` is set — one final `fix_run()` call with the full review context before human digest is posted.

**Tech Stack:** Python, `httpx` (already in deps), existing `HandoffReviewerSpec` / `_build_specs` patterns, `StrictBaseModel` config, `prepare_workdir` / `cleanup_workdir` / `commit_and_push` helpers from `backends._workdir` and `auto_fix`.

---

## File Map

| Action | Path | Responsibility |
|--------|------|----------------|
| Create | `src/caretaker/pr_reviewer/backends/openclaw_http.py` | SSE client, review prompt, `run()`, `fix_run()`, `SPEC` |
| Modify | `src/caretaker/config.py` | `OpenclaWHttpConfig` + `openclaw_http` field on `PRReviewerConfig` + `pre_escalation_agent` on `AutoFixConfig` |
| Modify | `src/caretaker/pr_reviewer/handoff_reviewer.py` | Register `openclaw_http.SPEC` in `_build_specs()` |
| Modify | `src/caretaker/pr_reviewer/auto_fix.py` | `dispatch_pre_escalation_attempt()` + `_resolve_backend_module` entry |
| Modify | `src/caretaker/pr_reviewer/agent.py` | Call `dispatch_pre_escalation_attempt` at max_attempts call sites |
| Create | `tests/test_pr_reviewer_openclaw_http_backend.py` | Tests for `run()`, `fix_run()`, SSE parse, error paths |
| Create | `tests/test_pr_reviewer_auto_fix_pre_escalation.py` | Tests for pre-escalation rung |

---

## Task 0: In-Cluster Validation (manual gate — run before writing any code)

**Files:** none (manual verification)

- [ ] **Step 1: Find the openclaw service**

```bash
kubectl get svc -A | grep -i openclaw
kubectl get pods -A | grep -i openclaw
```

Record: namespace, service name, cluster port.

- [ ] **Step 2: Confirm pod is healthy**

```bash
# Replace <namespace> and <svc> with values from Step 1
kubectl get endpoints -n <namespace> <svc>
kubectl get pods -n <namespace> -l app=openclaw
kubectl logs -n <namespace> -l app=openclaw --tail=50
```

Expected: ready endpoint IP, pod Running, HTTP listener log line.

- [ ] **Step 3: Port-forward and smoke-test the SSE endpoint**

```bash
# Terminal 1
kubectl port-forward -n <namespace> svc/<svc> 8080:<cluster-port>

# Terminal 2
curl -s http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "openclaw/default",
    "messages": [{"role": "user", "content": "Reply with one word: pong"}],
    "stream": true,
    "max_tokens": 10
  }' | head -20
```

Expected: lines of `data: {"choices":[{"delta":{"content":"..."}},...]}` ending with `data: [DONE]`.

- [ ] **Step 4: Record findings for config**

Fill in the findings table in the spec at `docs/superpowers/specs/2026-05-13-openclaw-agent-integration-design.md` (namespace, base URL, auth, model name, port). These values go into the YAML config — do not hardcode them.

- [ ] **Step 5: Gate check**

If any step above failed, stop. Do not proceed to code until openclaw is reachable and streaming works.

---

## Task 1: Config — `OpenclaWHttpConfig` and `AutoFixConfig.pre_escalation_agent`

**Files:**
- Modify: `src/caretaker/config.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_config_openclaw.py
from caretaker.config import AutoFixConfig, OpenclaWHttpConfig, PRReviewerConfig


def test_openclaw_http_config_defaults() -> None:
    cfg = OpenclaWHttpConfig()
    assert cfg.enabled is False
    assert cfg.base_url == ""
    assert cfg.api_key == ""
    assert cfg.model == "openclaw/default"
    assert cfg.timeout_seconds == 300
    assert cfg.keep_workdir_on_failure is False


def test_pr_reviewer_config_has_openclaw_http_field() -> None:
    cfg = PRReviewerConfig()
    assert hasattr(cfg, "openclaw_http")
    assert isinstance(cfg.openclaw_http, OpenclaWHttpConfig)


def test_auto_fix_config_has_pre_escalation_agent_field() -> None:
    cfg = AutoFixConfig()
    assert cfg.pre_escalation_agent == ""


def test_auto_fix_config_pre_escalation_agent_roundtrip() -> None:
    cfg = AutoFixConfig(pre_escalation_agent="openclaw_http")
    assert cfg.pre_escalation_agent == "openclaw_http"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /Users/ianlintner/Projects/caretaker
uv run pytest tests/test_config_openclaw.py -v
```

Expected: FAIL — `OpenclaWHttpConfig` not defined, `PRReviewerConfig` has no `openclaw_http`, `AutoFixConfig` has no `pre_escalation_agent`.

- [ ] **Step 3: Add `OpenclaWHttpConfig` to `config.py`**

In `src/caretaker/config.py`, after the `OpenCodeLocalBackendConfig` class (line ~928), add:

```python
class OpenclaWHttpConfig(StrictBaseModel):
    """Configuration for the ``openclaw_http`` review/fix backend.

    Calls the openclaw server's OpenAI-compatible
    ``/v1/chat/completions`` endpoint (SSE streaming) instead of
    spawning a subprocess. Designed for in-cluster deployments where
    openclaw runs as a Kubernetes service on a private network — no
    CLI binary required in caretaker's pod.
    """

    enabled: bool = False
    # In-cluster service URL, e.g.
    # ``http://openclaw.openclaw.svc.cluster.local:8080``.
    # Confirmed during Phase 0 validation; see the spec for the
    # port-forward smoke-test procedure.
    base_url: str = ""
    # Bearer token for the gateway auth. Empty string = open auth
    # (private in-cluster ingress — preferred for in-cluster).
    # When non-empty, store in ``caretaker-secrets`` / AKV under
    # ``openclaw-api-key`` and inject via env / secret mount.
    api_key: str = ""
    # Model identifier sent in the request body. Confirmed during
    # Phase 0 smoke-test (``openclaw/default`` is the typical value).
    model: str = "openclaw/default"
    timeout_seconds: int = 300
    keep_workdir_on_failure: bool = False
```

- [ ] **Step 4: Add `openclaw_http` field to `PRReviewerConfig`**

In `PRReviewerConfig` (line ~1064), after the `opencode_local` field:

```python
    # Settings for the openclaw HTTP backend. Used when
    # ``caretaker_owned_reviewer = "openclaw_http"`` or
    # ``complex_reviewer = "openclaw_http"``.
    openclaw_http: OpenclaWHttpConfig = Field(default_factory=OpenclaWHttpConfig)
```

- [ ] **Step 5: Add `pre_escalation_agent` to `AutoFixConfig`**

In `AutoFixConfig` (line ~960), after `always_run_heuristic`:

```python
    # When non-empty, this backend gets one final fix attempt after
    # ``max_attempts`` is exhausted — a last resort before caretaker
    # posts the human-escalation digest. Use ``"openclaw_http"`` to
    # route to the in-cluster openclaw server. Empty string disables
    # the pre-escalation rung (default).
    pre_escalation_agent: str = ""
```

- [ ] **Step 6: Run test to verify it passes**

```bash
uv run pytest tests/test_config_openclaw.py -v
```

Expected: PASS (4 tests).

- [ ] **Step 7: Run the full config test suite to catch regressions**

```bash
uv run pytest tests/ -k "config" -v
```

Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add src/caretaker/config.py tests/test_config_openclaw.py
git commit -m "feat(config): add OpenclaWHttpConfig and pre_escalation_agent field"
```

---

## Task 2: `openclaw_http` backend — SSE client and review `run()`

**Files:**
- Create: `src/caretaker/pr_reviewer/backends/openclaw_http.py`
- Create: `tests/test_pr_reviewer_openclaw_http_backend.py`

- [ ] **Step 1: Write failing tests for the SSE streaming helper and `run()`**

```python
# tests/test_pr_reviewer_openclaw_http_backend.py
"""Tests for the openclaw_http backend.

Heavy paths (clone + live HTTP) are out of scope. This file covers:
  * ``_collect_sse_text`` — SSE line parsing
  * ``_parse_review_payload`` — JSON block extraction + fallback
  * ``run()`` — happy path, HTTP error, timeout
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from caretaker.pr_reviewer.backends import openclaw_http
from caretaker.pr_reviewer.backends.openclaw_http import (
    OpenclawHttpError,
    _collect_sse_text,
    _parse_review_payload,
)


# ── _collect_sse_text ─────────────────────────────────────────────────────


def _sse_lines(chunks: list[str]) -> list[str]:
    """Build SSE line list from content chunks, as openclaw would stream them."""
    lines = [f'data: {{"choices":[{{"delta":{{"content":{chunk!r}}},"index":0}}]}}' for chunk in chunks]
    lines.append("data: [DONE]")
    return lines


@pytest.mark.asyncio
async def test_collect_sse_text_joins_chunks() -> None:
    lines = _sse_lines(["Hello", " world", "!"])

    async def fake_aiter():
        for line in lines:
            yield line

    result = await _collect_sse_text(fake_aiter())
    assert result == "Hello world!"


@pytest.mark.asyncio
async def test_collect_sse_text_skips_non_data_lines() -> None:
    async def fake_aiter():
        yield ": keep-alive"
        yield ""
        yield 'data: {"choices":[{"delta":{"content":"ok"},"index":0}]}'
        yield "data: [DONE]"

    result = await _collect_sse_text(fake_aiter())
    assert result == "ok"


@pytest.mark.asyncio
async def test_collect_sse_text_stops_at_done() -> None:
    async def fake_aiter():
        yield 'data: {"choices":[{"delta":{"content":"first"},"index":0}]}'
        yield "data: [DONE]"
        yield 'data: {"choices":[{"delta":{"content":"after"},"index":0}]}'

    result = await _collect_sse_text(fake_aiter())
    assert result == "first"


# ── _parse_review_payload ────────────────────────────────────────────────


def _review_block(verdict: str = "APPROVE") -> str:
    return (
        "Looks good.\n\n"
        "<!-- caretaker:review-result -->\n"
        "```caretaker-review\n"
        f'{{"verdict": "{verdict}", "summary": "lgtm", "comments": []}}\n'
        "```\n"
    )


def test_parse_review_payload_happy_path() -> None:
    result, fallback = _parse_review_payload(_review_block("APPROVE"))
    assert result.verdict == "APPROVE"
    assert fallback is False


def test_parse_review_payload_fallback_on_missing_block(caplog) -> None:
    with caplog.at_level("WARNING"):
        result, fallback = _parse_review_payload("Just prose, no JSON block.")
    assert result.verdict == "COMMENT"
    assert fallback is True
    assert any("fallback parse" in r.message for r in caplog.records)


def test_parse_review_payload_invalid_json_falls_back() -> None:
    bad = "<!-- caretaker:review-result -->\n```caretaker-review\nnot-json\n```\n"
    result, fallback = _parse_review_payload(bad)
    assert fallback is True


# ── run() ─────────────────────────────────────────────────────────────────


def _fake_config(
    base_url: str = "http://openclaw.test",
    model: str = "openclaw/default",
    api_key: str = "",
) -> MagicMock:
    cfg = MagicMock()
    cfg.base_url = base_url
    cfg.model = model
    cfg.api_key = api_key
    cfg.timeout_seconds = 30
    cfg.keep_workdir_on_failure = False
    return cfg


@pytest.mark.asyncio
async def test_run_returns_review_result_on_happy_path(monkeypatch) -> None:
    monkeypatch.setattr(
        openclaw_http,
        "_prepare_workdir",
        AsyncMock(return_value=("/tmp/fake", MagicMock())),
    )
    monkeypatch.setattr(openclaw_http, "cleanup_workdir", MagicMock())
    monkeypatch.setattr(
        openclaw_http,
        "_invoke_openclaw",
        AsyncMock(return_value=_review_block("REQUEST_CHANGES")),
    )

    result = await openclaw_http.run(
        pr_url="https://github.com/o/r/pull/1",
        config=_fake_config(),
    )
    assert result.verdict == "REQUEST_CHANGES"


@pytest.mark.asyncio
async def test_run_raises_openclaw_http_error_on_http_failure(monkeypatch) -> None:
    monkeypatch.setattr(
        openclaw_http,
        "_prepare_workdir",
        AsyncMock(return_value=("/tmp/fake", MagicMock())),
    )
    monkeypatch.setattr(openclaw_http, "cleanup_workdir", MagicMock())
    monkeypatch.setattr(
        openclaw_http,
        "_invoke_openclaw",
        AsyncMock(side_effect=OpenclawHttpError("HTTP 502")),
    )

    with pytest.raises(OpenclawHttpError):
        await openclaw_http.run(
            pr_url="https://github.com/o/r/pull/1",
            config=_fake_config(),
        )


@pytest.mark.asyncio
async def test_run_returns_fallback_result_when_no_json_block(monkeypatch) -> None:
    monkeypatch.setattr(
        openclaw_http,
        "_prepare_workdir",
        AsyncMock(return_value=("/tmp/fake", MagicMock())),
    )
    monkeypatch.setattr(openclaw_http, "cleanup_workdir", MagicMock())
    monkeypatch.setattr(
        openclaw_http,
        "_invoke_openclaw",
        AsyncMock(return_value="Just prose with no JSON block."),
    )

    result = await openclaw_http.run(
        pr_url="https://github.com/o/r/pull/1",
        config=_fake_config(),
    )
    assert result.verdict == "COMMENT"
```

- [ ] **Step 2: Run to verify failure**

```bash
uv run pytest tests/test_pr_reviewer_openclaw_http_backend.py -v
```

Expected: FAIL — module `openclaw_http` not found.

- [ ] **Step 3: Create `src/caretaker/pr_reviewer/backends/openclaw_http.py`**

```python
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
from typing import TYPE_CHECKING, AsyncIterator

import httpx

from caretaker.pr_reviewer.backends._workdir import (
    WorkdirError,
    cleanup_workdir,
    parse_pr_url,
    prepare_workdir,
)
from caretaker.pr_reviewer.handoff_reviewer import (
    OPENCLAW_HTTP_REVIEW_MARKER,
    HandoffReviewerSpec,
)
from caretaker.pr_reviewer.inline_reviewer import InlineReviewComment, ReviewResult

if TYPE_CHECKING:
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

  1. A short prose summary (2–6 sentences).

  2. The exact marker ``<!-- caretaker:review-result -->`` on its own
     line, then a fenced JSON block tagged ``caretaker-review``:

     ```caretaker-review
     {
       "verdict": "APPROVE" | "COMMENT" | "REQUEST_CHANGES",
       "summary": "1–3 sentence overall assessment",
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
        async with httpx.AsyncClient(timeout=float(timeout_seconds)) as client:
            async with client.stream("POST", url, json=body, headers=headers) as response:
                if response.status_code >= 400:
                    body_text = await response.aread()
                    raise OpenclawHttpError(
                        f"openclaw returned HTTP {response.status_code}: "
                        f"{body_text.decode()[:400]}"
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
    COMMENT-verdict fallback so the review doesn't silently vanish.
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
            ),
            True,
        )

    try:
        payload = json.loads(m.group("json"))
    except json.JSONDecodeError as exc:
        logger.warning("openclaw_http: malformed JSON in caretaker-review block: %s", exc)
        return (
            ReviewResult(summary="openclaw_http: malformed JSON in review block.", verdict="COMMENT"),
            True,
        )

    if not isinstance(payload, dict):
        return ReviewResult(summary="openclaw_http: review payload is not a JSON object.", verdict="COMMENT"), True

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
            if isinstance(path, str) and path and isinstance(line, int) and line > 0 and isinstance(body, str) and body.strip():
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


# Reusable helpers so the workdir setup step is mockable in tests
# (same pattern as opencode_local._prepare_workdir).
async def _prepare_workdir(
    pr_url: str,
    *,
    config: OpenclaWHttpConfig,
    head_branch: str | None = None,
) -> tuple[str, object]:
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
    assistant's summary text or raises ``OpenclawHttpError`` / on the
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
```

- [ ] **Step 4: Add `OPENCLAW_HTTP_REVIEW_MARKER` to `handoff_reviewer.py`**

In `src/caretaker/pr_reviewer/handoff_reviewer.py`, after the existing marker constants (line ~37), add:

```python
OPENCLAW_HTTP_REVIEW_MARKER = "<!-- caretaker:pr-reviewer-openclaw-http-handoff -->"
```

Also add it to `__all__` at the bottom of the file.

- [ ] **Step 5: Run tests to verify they pass**

```bash
uv run pytest tests/test_pr_reviewer_openclaw_http_backend.py -v
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add src/caretaker/pr_reviewer/backends/openclaw_http.py \
        src/caretaker/pr_reviewer/handoff_reviewer.py \
        tests/test_pr_reviewer_openclaw_http_backend.py
git commit -m "feat(pr_reviewer): add openclaw_http backend with SSE streaming"
```

---

## Task 3: Register `openclaw_http` SPEC and `fix_run` resolver

**Files:**
- Modify: `src/caretaker/pr_reviewer/handoff_reviewer.py` — `_build_specs()`
- Modify: `src/caretaker/pr_reviewer/auto_fix.py` — `_resolve_backend_module()`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_openclaw_http_registration.py
from caretaker.pr_reviewer.handoff_reviewer import get_spec, list_backends


def test_openclaw_http_spec_registered() -> None:
    spec = get_spec("openclaw_http")
    assert spec.backend == "openclaw_http"
    assert spec.invocation == "local_subprocess"
    assert spec.runner is not None


def test_openclaw_http_listed_in_backends() -> None:
    assert "openclaw_http" in list_backends()
```

```python
# In tests/test_pr_reviewer_auto_fix.py (add to existing file):
from caretaker.pr_reviewer.auto_fix import _resolve_backend_module


def test_resolve_backend_module_openclaw_http() -> None:
    mod = _resolve_backend_module("openclaw_http")
    assert hasattr(mod, "fix_run")
```

- [ ] **Step 2: Run to verify failure**

```bash
uv run pytest tests/test_openclaw_http_registration.py -v
uv run pytest tests/test_pr_reviewer_auto_fix.py::test_resolve_backend_module_openclaw_http -v
```

Expected: FAIL — `get_spec("openclaw_http")` raises ValueError; `_resolve_backend_module` raises WorkdirError.

- [ ] **Step 3: Register SPEC in `_build_specs()`**

In `src/caretaker/pr_reviewer/handoff_reviewer.py`, in `_build_specs()` (line ~109), add `openclaw_http` to the import and the loop:

```python
    from caretaker.pr_reviewer.backends import (
        claude_code_local,
        coderabbit,
        greptile,
        openclaw_http,       # ← add
        opencode_local,
        pr_agent,
    )

    for spec in (
        pr_agent.SPEC,
        coderabbit.SPEC,
        greptile.SPEC,
        claude_code_local.SPEC,
        opencode_local.SPEC,
        openclaw_http.SPEC,  # ← add
    ):
        specs[spec.backend] = spec
```

- [ ] **Step 4: Register in `_resolve_backend_module()`**

In `src/caretaker/pr_reviewer/auto_fix.py`, in `_resolve_backend_module()` (line ~661), add before the final `raise`:

```python
    if backend == "openclaw_http":
        from caretaker.pr_reviewer.backends import openclaw_http  # noqa: PLC0415

        return openclaw_http
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
uv run pytest tests/test_openclaw_http_registration.py tests/test_pr_reviewer_auto_fix.py::test_resolve_backend_module_openclaw_http -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/caretaker/pr_reviewer/handoff_reviewer.py \
        src/caretaker/pr_reviewer/auto_fix.py \
        tests/test_openclaw_http_registration.py
git commit -m "feat(pr_reviewer): register openclaw_http in spec registry and backend resolver"
```

---

## Task 4: Pre-escalation rung — `dispatch_pre_escalation_attempt()`

**Files:**
- Modify: `src/caretaker/pr_reviewer/auto_fix.py`
- Create: `tests/test_pr_reviewer_auto_fix_pre_escalation.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_pr_reviewer_auto_fix_pre_escalation.py
"""Tests for the pre-escalation rung added to auto_fix.py."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from caretaker.config import AutoFixConfig, OpenclaWHttpConfig, PRReviewerConfig
from caretaker.pr_reviewer.auto_fix import dispatch_pre_escalation_attempt
from caretaker.pr_reviewer.inline_reviewer import ReviewResult
from caretaker.state.models import TrackedPR


def _review(verdict: str = "REQUEST_CHANGES") -> ReviewResult:
    return ReviewResult(summary="bad code", verdict=verdict, comments=[])


def _tracking(attempts: int = 3) -> TrackedPR:
    return TrackedPR(number=42, auto_fix_attempts=attempts)


def _cfg(pre_escalation_agent: str = "openclaw_http") -> PRReviewerConfig:
    cfg = PRReviewerConfig()
    cfg.auto_fix.pre_escalation_agent = pre_escalation_agent
    cfg.openclaw_http.enabled = True
    cfg.openclaw_http.base_url = "http://openclaw.test"
    cfg.openclaw_http.model = "openclaw/default"
    cfg.openclaw_http.timeout_seconds = 30
    return cfg


@pytest.mark.asyncio
async def test_pre_escalation_returns_none_when_agent_not_configured() -> None:
    result = await dispatch_pre_escalation_attempt(
        review=_review(),
        config=_cfg(pre_escalation_agent=""),
        github=MagicMock(),
        owner="o",
        repo="r",
        pr_number=42,
        pr_url="https://github.com/o/r/pull/42",
        head_branch="feat",
        tracking=_tracking(),
    )
    assert result is None


@pytest.mark.asyncio
async def test_pre_escalation_returns_new_head_sha_on_success(monkeypatch) -> None:
    import caretaker.pr_reviewer.auto_fix as af

    monkeypatch.setattr(af, "_resolve_backend_module", lambda name: MagicMock(
        fix_run=AsyncMock(return_value="patched the bug"),
    ))
    monkeypatch.setattr(
        "caretaker.pr_reviewer.auto_fix.prepare_workdir",
        AsyncMock(return_value=("/tmp/fake", MagicMock())),
    )
    monkeypatch.setattr(
        "caretaker.pr_reviewer.auto_fix.commit_and_push",
        AsyncMock(return_value="abc1234"),
    )
    monkeypatch.setattr(
        "caretaker.pr_reviewer.auto_fix.cleanup_workdir",
        MagicMock(),
    )

    import os
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")

    result = await dispatch_pre_escalation_attempt(
        review=_review(),
        config=_cfg(),
        github=MagicMock(),
        owner="o",
        repo="r",
        pr_number=42,
        pr_url="https://github.com/o/r/pull/42",
        head_branch="feat",
        tracking=_tracking(),
    )
    assert result == "abc1234"


@pytest.mark.asyncio
async def test_pre_escalation_returns_none_on_fix_declined(monkeypatch) -> None:
    from caretaker.pr_reviewer.backends.openclaw_http import OpenclawHttpError
    import caretaker.pr_reviewer.auto_fix as af

    monkeypatch.setattr(af, "_resolve_backend_module", lambda name: MagicMock(
        fix_run=AsyncMock(side_effect=OpenclawHttpError("openclaw declined to fix: reason")),
    ))
    monkeypatch.setattr(
        "caretaker.pr_reviewer.auto_fix.prepare_workdir",
        AsyncMock(return_value=("/tmp/fake", MagicMock())),
    )
    monkeypatch.setattr("caretaker.pr_reviewer.auto_fix.cleanup_workdir", MagicMock())
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")

    result = await dispatch_pre_escalation_attempt(
        review=_review(),
        config=_cfg(),
        github=MagicMock(),
        owner="o",
        repo="r",
        pr_number=42,
        pr_url="https://github.com/o/r/pull/42",
        head_branch="feat",
        tracking=_tracking(),
    )
    assert result is None
```

- [ ] **Step 2: Run to verify failure**

```bash
uv run pytest tests/test_pr_reviewer_auto_fix_pre_escalation.py -v
```

Expected: FAIL — `dispatch_pre_escalation_attempt` not importable.

- [ ] **Step 3: Implement `dispatch_pre_escalation_attempt()` in `auto_fix.py`**

In `src/caretaker/pr_reviewer/auto_fix.py`, after `_resolve_backend_module()` and before `__all__`, add:

```python
async def dispatch_pre_escalation_attempt(
    *,
    review: ReviewResult,
    config: PRReviewerConfig,
    github: GitHubClient,
    owner: str,
    repo: str,
    pr_number: int,
    pr_url: str,
    head_branch: str,
    tracking: TrackedPR,
) -> str | None:
    """Make one final fix attempt via the configured pre-escalation agent.

    Called after the normal fix loop exhausts ``max_attempts``, before
    the human-escalation digest is posted.  Returns the new head SHA on
    success, or ``None`` if the agent declined or errored (caller falls
    through to human digest).

    Uses ``fix_run()`` from the named backend module (same path as
    ``_dispatch_auto_fix_inner``) but passes the full review context and
    the accumulated attempt count so the agent knows it's the last shot.
    """
    agent_name = config.auto_fix.pre_escalation_agent
    if not agent_name:
        return None

    import os as _os  # noqa: PLC0415

    from caretaker.pr_reviewer.backends._workdir import (
        WorkdirError,
        prepare_workdir,
    )

    token = _os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        logger.warning(
            "pre_escalation: GITHUB_TOKEN not set; cannot push fix commits — skipping"
        )
        return None

    workdir: str | None = None
    try:
        backend_module = _resolve_backend_module(agent_name)
        fix_callable = getattr(backend_module, "fix_run", None)
        if fix_callable is None:
            logger.warning(
                "pre_escalation: backend %r has no fix_run(); skipping", agent_name
            )
            return None

        backend_config = getattr(config, agent_name, None)
        workdir, _parsed = await prepare_workdir(
            pr_url,
            clone_depth=50,
            head_branch=head_branch,
            github_token=token,
        )

        await fix_callable(
            workdir=workdir,
            review_summary=review.summary,
            review_comments=review.comments,
            config=backend_config,
            prior_errors=review.summary,
            attempt_count=int(tracking.auto_fix_attempts),
        )

        new_head = await commit_and_push(
            workdir=workdir,
            branch=head_branch,
            commit_message=(
                "fix: pre-escalation attempt by openclaw "
                f"(after {tracking.auto_fix_attempts} failed attempts) "
                "[caretaker auto-fix]"
            ),
            github_token=token,
            owner=owner,
            repo=repo,
        )
        logger.info(
            "pre_escalation: %s succeeded on PR #%d → %s",
            agent_name, pr_number, new_head[:8],
        )
        return new_head
    except Exception as exc:  # noqa: BLE001 — pre-escalation is best-effort
        logger.warning(
            "pre_escalation: %s failed on PR #%d: %s",
            agent_name, pr_number, exc,
        )
        return None
    finally:
        if workdir is not None:
            with contextlib.suppress(Exception):  # noqa: BLE001
                from caretaker.pr_reviewer.backends._workdir import (  # noqa: PLC0415
                    cleanup_workdir as _cleanup,
                )
                _cleanup(workdir, keep=False)
```

Also add `"dispatch_pre_escalation_attempt"` to `__all__` in `auto_fix.py`.

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/test_pr_reviewer_auto_fix_pre_escalation.py -v
```

Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add src/caretaker/pr_reviewer/auto_fix.py \
        tests/test_pr_reviewer_auto_fix_pre_escalation.py
git commit -m "feat(auto_fix): add dispatch_pre_escalation_attempt for last-resort openclaw fix"
```

---

## Task 5: Wire the pre-escalation call site in `agent.py`

**Files:**
- Modify: `src/caretaker/pr_reviewer/agent.py`

The pre-escalation fires at every place in `agent.py` where `decide_auto_fix` returns `should_dispatch=False` with `"max_attempts"` in the reason. There are three call sites (lines ~470, ~694, ~869). The same pattern applies to each.

- [ ] **Step 1: Write the failing test**

```python
# In tests/test_pr_reviewer_auto_fix_pre_escalation.py — add this test:

@pytest.mark.asyncio
async def test_should_attempt_pre_escalation_when_max_attempts_reached() -> None:
    """Helper used by agent.py: pre-escalation triggers when reason contains max_attempts."""
    from caretaker.pr_reviewer.auto_fix import should_attempt_pre_escalation

    auto_fix_cfg_with_agent = AutoFixConfig(pre_escalation_agent="openclaw_http")
    auto_fix_cfg_empty = AutoFixConfig(pre_escalation_agent="")

    assert should_attempt_pre_escalation("max_attempts=3 reached; escalate", auto_fix_cfg_with_agent) is True
    assert should_attempt_pre_escalation("verdict APPROVE is not REQUEST_CHANGES", auto_fix_cfg_with_agent) is False
    assert should_attempt_pre_escalation("max_attempts=3 reached; escalate", auto_fix_cfg_empty) is False
```

- [ ] **Step 2: Run to verify failure**

```bash
uv run pytest tests/test_pr_reviewer_auto_fix_pre_escalation.py::test_should_attempt_pre_escalation_when_max_attempts_reached -v
```

Expected: FAIL — `should_attempt_pre_escalation` not importable.

- [ ] **Step 3: Add `should_attempt_pre_escalation()` helper to `auto_fix.py`**

In `src/caretaker/pr_reviewer/auto_fix.py`, just before `dispatch_pre_escalation_attempt`, add:

```python
def should_attempt_pre_escalation(decide_reason: str, auto_fix_cfg: AutoFixConfig) -> bool:
    """Return True when the fix loop hit its cap and a pre-escalation agent is configured."""
    return bool(auto_fix_cfg.pre_escalation_agent) and "max_attempts" in decide_reason
```

Add `"should_attempt_pre_escalation"` to `__all__`.

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run pytest tests/test_pr_reviewer_auto_fix_pre_escalation.py -v
```

Expected: all pass.

- [ ] **Step 5: Wire the three call sites in `agent.py`**

There are three places in `src/caretaker/pr_reviewer/agent.py` where `fix_decision.should_dispatch` is False. At each site, after `auto_fix_reason = fix_decision.reason`, add the pre-escalation dispatch:

**Pattern to apply at each of the three call sites (lines ~478, ~701, ~877):**

Before this pattern exists:
```python
                        else:
                            auto_fix_reason = fix_decision.reason
```

Replace with (the `else` branch that currently just records the reason):
```python
                        else:
                            auto_fix_reason = fix_decision.reason
                            if _auto_fix.should_attempt_pre_escalation(
                                fix_decision.reason, cfg.auto_fix
                            ):
                                new_sha = await _auto_fix.dispatch_pre_escalation_attempt(
                                    review=review_result,  # adjust var name per call site
                                    config=cfg,
                                    github=self._ctx.github,
                                    owner=owner,
                                    repo=repo,
                                    pr_number=pr_number,
                                    pr_url=pr_url,
                                    head_branch=head_branch,
                                    tracking=tracking,  # adjust var name per call site
                                )
                                if new_sha:
                                    auto_fix_dispatched = True
                                    auto_fix_reason = (
                                        f"pre-escalation via {cfg.auto_fix.pre_escalation_agent} → {new_sha[:8]}"
                                    )
```

**Important:** the variable names differ slightly at each call site. Check the surrounding code carefully:
- Call site 1 (harvested reviews, ~line 470): `review_result` is the loop variable; `tracking` is the tracking object; `pr_url` comes from `pr.get("html_url", "")`.
- Call site 2 (webhook path, ~line 694): same pattern; check exact variable names.
- Call site 3 (caretaker-owned PR path, ~line 869): `local_result` is the review; `_tracking` is the tracking object; `pr_url` is constructed.

Read each block carefully before editing and match the local variable names.

- [ ] **Step 6: Run the full pr_reviewer agent test suite**

```bash
uv run pytest tests/test_pr_reviewer.py tests/test_pr_reviewer_auto_fix.py tests/test_pr_reviewer_auto_fix_pre_escalation.py -v
```

Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add src/caretaker/pr_reviewer/agent.py \
        src/caretaker/pr_reviewer/auto_fix.py \
        tests/test_pr_reviewer_auto_fix_pre_escalation.py
git commit -m "feat(agent): wire pre-escalation openclaw attempt before human digest"
```

---

## Task 6: Type-check and full test run

**Files:** none — validation only

- [ ] **Step 1: Run mypy on the changed modules**

```bash
uv run mypy src/caretaker/pr_reviewer/backends/openclaw_http.py \
            src/caretaker/pr_reviewer/auto_fix.py \
            src/caretaker/config.py \
            src/caretaker/pr_reviewer/agent.py \
            --ignore-missing-imports 2>&1 | tail -20
```

Expected: no new errors beyond any pre-existing baseline.

- [ ] **Step 2: Run the full test suite**

```bash
uv run pytest tests/ -x -q 2>&1 | tail -30
```

Expected: all pass (or only pre-existing failures unrelated to this feature).

- [ ] **Step 3: Commit if any type-check fixes were needed**

```bash
git add -p
git commit -m "fix(openclaw_http): type annotation corrections from mypy"
```

---

## Self-Review Checklist

**Spec coverage:**
- [x] Phase 0 in-cluster validation — Task 0
- [x] `OpenclaWHttpConfig` — Task 1
- [x] `pre_escalation_agent` on `AutoFixConfig` — Task 1
- [x] `openclaw_http` field on `PRReviewerConfig` — Task 1
- [x] `openclaw_http` review backend with SSE — Task 2
- [x] `OPENCLAW_HTTP_REVIEW_MARKER` in `handoff_reviewer.py` — Task 2
- [x] `fix_run()` with pre-escalation prompt variant — Task 2
- [x] SPEC registered in `_build_specs()` — Task 3
- [x] `_resolve_backend_module` entry — Task 3
- [x] `dispatch_pre_escalation_attempt()` — Task 4
- [x] `should_attempt_pre_escalation()` helper — Task 5
- [x] Three `agent.py` call sites wired — Task 5
- [x] Tests for all units — Tasks 1–5

**Out of scope (per spec):** gRPC, `/tools/invoke` MCP surface, complexity-classifier fast-lane, `CodingAgent` / `ExecutorDispatcher` path (pre-escalation uses `fix_run` directly, consistent with existing backends).
