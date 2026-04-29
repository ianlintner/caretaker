"""Tests for the claude_code_local backend.

The clone + claude-CLI invocation paths require real binaries and are
out of scope for unit tests; they're exercised in integration runs. The
parts under test here are the pure-Python ones: PR-URL parsing, the
stream-json event extractor, the JSON-block parser, and the spec/config
wiring.
"""

from __future__ import annotations

import json

import pytest

from caretaker.config import ClaudeCodeLocalBackendConfig, PRReviewerConfig
from caretaker.pr_reviewer import handoff_reviewer
from caretaker.pr_reviewer.backends import claude_code_local
from caretaker.pr_reviewer.backends.claude_code_local import (
    ClaudeCodeLocalError,
    _extract_assistant_text,
    _parse_pr_url,
    _parse_review_payload,
)

# ── PR-URL parsing ────────────────────────────────────────────────────────


def test_parse_pr_url_browser_form() -> None:
    parsed = _parse_pr_url("https://github.com/owner/repo/pull/42")
    assert (parsed.owner, parsed.repo, parsed.number) == ("owner", "repo", 42)


def test_parse_pr_url_api_form() -> None:
    parsed = _parse_pr_url("https://api.github.com/repos/o/r/pulls/7")
    assert (parsed.owner, parsed.repo, parsed.number) == ("o", "r", 7)


def test_parse_pr_url_invalid_raises() -> None:
    with pytest.raises(ClaudeCodeLocalError, match="cannot parse"):
        _parse_pr_url("https://github.com/owner/repo/issues/42")


# ── stream-json event extractor ───────────────────────────────────────────


def _make_stream_json(events: list[dict]) -> str:
    return "\n".join(json.dumps(e) for e in events) + "\n"


def test_extract_assistant_text_prefers_result_event() -> None:
    stream = _make_stream_json(
        [
            {"type": "system", "subtype": "init"},
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "thinking..."}]},
            },
            {"type": "result", "result": "FINAL_TEXT", "subtype": "success"},
        ]
    )
    assert _extract_assistant_text(stream) == "FINAL_TEXT"


def test_extract_assistant_text_falls_back_to_assistant_chunks() -> None:
    """When no `result` event arrives, concatenate assistant text blocks."""
    stream = _make_stream_json(
        [
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "part A"}]},
            },
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "part B"}]},
            },
        ]
    )
    assert _extract_assistant_text(stream) == "part A\npart B"


def test_extract_assistant_text_skips_malformed_lines() -> None:
    stream = "not json\n" + json.dumps({"type": "result", "result": "OK"}) + "\nalso not json\n"
    assert _extract_assistant_text(stream) == "OK"


# ── JSON-block parser ────────────────────────────────────────────────────


def test_parse_review_payload_extracts_json_block() -> None:
    text = (
        "Some prose summary first.\n\n"
        "<!-- caretaker:review-result -->\n"
        "```caretaker-review\n"
        '{"verdict": "REQUEST_CHANGES", "summary": "auth bug", '
        '"comments": [{"path": "src/x.py", "line": 5, "body": "fix"}]}\n'
        "```\n"
    )
    result = _parse_review_payload(text)
    assert result.verdict == "REQUEST_CHANGES"
    assert "auth bug" in result.summary
    assert len(result.comments) == 1
    assert result.comments[0].path == "src/x.py"
    assert result.comments[0].line == 5


def test_parse_review_payload_missing_block_falls_back_to_comment() -> None:
    """A claude reply without the fence still produces some review."""
    text = "Just prose, no JSON. Looks good to me."
    result = _parse_review_payload(text)
    assert result.verdict == "COMMENT"
    assert "Just prose" in result.summary
    assert result.comments == []


def test_parse_review_payload_invalid_verdict_defaults_to_comment() -> None:
    text = (
        "```caretaker-review\n"
        '{"verdict": "WAFFLE", "summary": "weird verdict", "comments": []}\n'
        "```\n"
    )
    result = _parse_review_payload(text)
    assert result.verdict == "COMMENT"


def test_parse_review_payload_filters_invalid_comments() -> None:
    """Comments missing path/line/body are dropped, not crashed on."""
    text = (
        "```caretaker-review\n"
        + json.dumps(
            {
                "verdict": "COMMENT",
                "summary": "ok",
                "comments": [
                    {"path": "src/a.py", "line": 1, "body": "good"},
                    {"path": "", "line": 2, "body": "bad-empty-path"},
                    {"path": "src/b.py", "line": 0, "body": "bad-line"},
                    {"path": "src/c.py", "line": 3, "body": ""},
                    "not-a-dict",
                ],
            }
        )
        + "\n```\n"
    )
    result = _parse_review_payload(text)
    assert len(result.comments) == 1
    assert result.comments[0].path == "src/a.py"


def test_parse_review_payload_caps_comments_at_eight() -> None:
    comments = [{"path": f"src/f{i}.py", "line": 1, "body": f"c{i}"} for i in range(20)]
    text = (
        "```caretaker-review\n"
        + json.dumps({"verdict": "COMMENT", "summary": "ok", "comments": comments})
        + "\n```\n"
    )
    result = _parse_review_payload(text)
    assert len(result.comments) == 8


def test_parse_review_payload_invalid_json_raises() -> None:
    text = "```caretaker-review\n{not-json}\n```\n"
    with pytest.raises(ClaudeCodeLocalError, match="not valid JSON"):
        _parse_review_payload(text)


# ── registry / spec wiring ───────────────────────────────────────────────


def test_spec_is_registered_as_local_subprocess() -> None:
    spec = handoff_reviewer.get_spec("claude_code_local")
    assert spec.invocation == "local_subprocess"
    assert spec.runner is claude_code_local.run
    assert spec.marker == handoff_reviewer.CLAUDE_CODE_LOCAL_REVIEW_MARKER
    assert "claude_code_local" in handoff_reviewer.known_backends()


def test_marker_is_distinct_from_claude_code_action_marker() -> None:
    """The two claude-flavoured backends must not share a marker (prevents harvest mix-up)."""
    assert (
        handoff_reviewer.CLAUDE_CODE_LOCAL_REVIEW_MARKER
        != handoff_reviewer.CLAUDE_CODE_REVIEW_MARKER
    )


def test_claude_code_local_opt_in_only_by_default() -> None:
    """Backend ships registered but not enabled — needs explicit opt-in."""
    cfg = PRReviewerConfig()
    assert "claude_code_local" not in cfg.enabled_backends
    # The config block exists with safe defaults, though.
    assert isinstance(cfg.claude_code_local, ClaudeCodeLocalBackendConfig)
    assert cfg.claude_code_local.permission_mode == "plan"
    assert "Read" in cfg.claude_code_local.allowed_tools
