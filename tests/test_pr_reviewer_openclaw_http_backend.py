"""Tests for the openclaw_http backend.

Heavy paths (clone + live HTTP) are out of scope. This file covers:
  * ``_collect_sse_text`` — SSE line parsing
  * ``_parse_review_payload`` — JSON block extraction + fallback
  * ``run()`` — happy path, HTTP error, timeout
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

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
    lines = [
        f'data: {{"choices":[{{"delta":{{"content":{chunk!r}}},"index":0}}]}}' for chunk in chunks
    ]
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


@pytest.mark.asyncio
async def test_run_raises_on_timeout(monkeypatch) -> None:
    monkeypatch.setattr(
        openclaw_http,
        "_prepare_workdir",
        AsyncMock(return_value=("/tmp/fake", MagicMock())),
    )
    monkeypatch.setattr(openclaw_http, "cleanup_workdir", MagicMock())
    monkeypatch.setattr(
        openclaw_http,
        "_invoke_openclaw",
        AsyncMock(side_effect=OpenclawHttpError("openclaw timed out after 30s")),
    )

    with pytest.raises(OpenclawHttpError, match="timed out"):
        await openclaw_http.run(
            pr_url="https://github.com/o/r/pull/1",
            config=_fake_config(),
        )


# ── fix_run() ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fix_run_returns_summary_on_success(monkeypatch) -> None:
    monkeypatch.setattr(
        openclaw_http,
        "_invoke_openclaw",
        AsyncMock(return_value="Applied the suggested changes successfully."),
    )

    result = await openclaw_http.fix_run(
        workdir="/tmp/fake",
        review_summary="Fix the type error in foo.py",
        review_comments=[],
        config=_fake_config(),
    )
    assert result == "Applied the suggested changes successfully."


@pytest.mark.asyncio
async def test_fix_run_raises_on_cannot_fix(monkeypatch) -> None:
    monkeypatch.setattr(
        openclaw_http,
        "_invoke_openclaw",
        AsyncMock(return_value="CARETAKER_FIX_DECLINED: The issue requires manual intervention."),
    )

    with pytest.raises(OpenclawHttpError, match="declined to fix"):
        await openclaw_http.fix_run(
            workdir="/tmp/fake",
            review_summary="Fix the type error in foo.py",
            review_comments=[],
            config=_fake_config(),
        )


# ── _parse_review_payload fallback inline comment ─────────────────────────


def test_parse_review_payload_fallback_includes_inline_comment() -> None:
    result, fallback = _parse_review_payload("Just prose, no JSON block.")
    assert fallback is True
    assert len(result.comments) >= 1
    assert any("parse" in c.body for c in result.comments)
