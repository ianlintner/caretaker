"""Tests for the pr-agent CLI backend (subprocess wrapper + transformer)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from caretaker.config import PRAgentBackendConfig, PRReviewerConfig
from caretaker.pr_reviewer import handoff_reviewer
from caretaker.pr_reviewer.backends import pr_agent as pr_agent_backend
from caretaker.pr_reviewer.backends.pr_agent import (
    PRAgentInvocationError,
    PRAgentRawResult,
    to_caretaker_review,
)

# ── transformer (pure functions, no I/O) ───────────────────────────────────


def test_transformer_uses_first_paragraph_for_summary() -> None:
    raw = PRAgentRawResult(
        stdout=(
            "## PR Reviewer Guide\n\n"
            "This PR refactors the auth middleware to use the new session store. "
            "The change is contained and well-tested.\n\n"
            "## PR Code Suggestions\n"
        ),
        stderr="",
        returncode=0,
    )
    result = to_caretaker_review(raw, include_full_output=False)
    assert "refactors the auth middleware" in result.summary
    # Default verdict for non-security output is COMMENT.
    assert result.verdict == "COMMENT"
    assert result.comments == []


def test_transformer_marks_security_findings_as_request_changes() -> None:
    raw = PRAgentRawResult(
        stdout=(
            "## PR Reviewer Guide\n\n"
            "🔒 Security concern: the new endpoint accepts user input without validation, "
            "creating an XSS surface in the rendered template.\n"
        ),
        stderr="",
        returncode=0,
    )
    result = to_caretaker_review(raw, include_full_output=False)
    assert result.verdict == "REQUEST_CHANGES"


def test_transformer_extracts_inline_comments_from_suggestions_table() -> None:
    raw = PRAgentRawResult(
        stdout=(
            "## PR Code Suggestions\n"
            "| relevant file | suggestion |\n"
            "|---|---|\n"
            "| src/auth/login.py | Use constant-time comparison for the token check |\n"
            "| src/api/handlers.py | Validate request body before logging |\n"
        ),
        stderr="",
        returncode=0,
    )
    result = to_caretaker_review(raw, include_full_output=False)
    assert len(result.comments) == 2
    assert result.comments[0].path == "src/auth/login.py"
    assert "constant-time" in result.comments[0].body
    assert result.comments[1].path == "src/api/handlers.py"


def test_transformer_caps_inline_comments_at_eight() -> None:
    rows = "\n".join(f"| src/f{i}.py | suggestion {i} |" for i in range(20))
    raw = PRAgentRawResult(
        stdout=f"## PR Code Suggestions\n| relevant file | suggestion |\n|---|---|\n{rows}\n",
        stderr="",
        returncode=0,
    )
    result = to_caretaker_review(raw, include_full_output=False)
    assert len(result.comments) == 8


def test_transformer_includes_full_output_when_requested() -> None:
    raw = PRAgentRawResult(stdout="A short body.\n", stderr="", returncode=0)
    expanded = to_caretaker_review(raw, include_full_output=True)
    bare = to_caretaker_review(raw, include_full_output=False)
    assert "<details>" in expanded.summary
    assert "<details>" not in bare.summary


# ── subprocess wrapper (mocked) ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_pr_agent_raises_on_missing_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pr_agent_backend.shutil, "which", lambda _: None)
    with pytest.raises(PRAgentInvocationError, match="not found"):
        await pr_agent_backend.run_pr_agent(
            pr_url="https://github.com/o/r/pull/1",
            cli_path="definitely-not-a-binary-xyz",
        )


@pytest.mark.asyncio
async def test_run_pr_agent_raises_on_nonzero_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pr_agent_backend.shutil, "which", lambda x: f"/usr/bin/{x}")

    fake_proc = MagicMock()
    fake_proc.returncode = 2
    fake_proc.communicate = AsyncMock(return_value=(b"", b"boom: bad config"))

    async def _fake_create(*args, **kwargs):
        return fake_proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create)
    with pytest.raises(PRAgentInvocationError, match="exited 2"):
        await pr_agent_backend.run_pr_agent(
            pr_url="https://github.com/o/r/pull/1",
            cli_path="pr-agent",
        )


@pytest.mark.asyncio
async def test_run_pr_agent_returns_stdout_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pr_agent_backend.shutil, "which", lambda x: f"/usr/bin/{x}")

    fake_proc = MagicMock()
    fake_proc.returncode = 0
    fake_proc.communicate = AsyncMock(return_value=(b"## Review\n\nLooks good.\n", b""))

    async def _fake_create(*args, **kwargs):
        return fake_proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create)
    raw = await pr_agent_backend.run_pr_agent(
        pr_url="https://github.com/o/r/pull/1", cli_path="pr-agent"
    )
    assert raw.returncode == 0
    assert "Looks good" in raw.stdout


@pytest.mark.asyncio
async def test_run_pr_agent_raises_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pr_agent_backend.shutil, "which", lambda x: f"/usr/bin/{x}")

    fake_proc = MagicMock()
    fake_proc.returncode = None

    async def _slow_communicate():
        await asyncio.sleep(10)
        return (b"", b"")

    fake_proc.communicate = AsyncMock(side_effect=_slow_communicate)
    fake_proc.kill = MagicMock()

    async def _fake_create(*args, **kwargs):
        return fake_proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create)
    with pytest.raises(PRAgentInvocationError, match="timed out"):
        await pr_agent_backend.run_pr_agent(
            pr_url="https://github.com/o/r/pull/1",
            cli_path="pr-agent",
            timeout_seconds=0,
        )
    fake_proc.kill.assert_called_once()


# ── high-level run() coroutine wires wrapper + transformer ────────────────


@pytest.mark.asyncio
async def test_run_returns_review_result(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_run_pr_agent(**_: object) -> PRAgentRawResult:
        return PRAgentRawResult(
            stdout="## Review\n\nLooks good overall.\n", stderr="", returncode=0
        )

    monkeypatch.setattr(pr_agent_backend, "run_pr_agent", _fake_run_pr_agent)
    cfg = PRAgentBackendConfig()
    result = await pr_agent_backend.run(pr_url="https://github.com/o/r/pull/1", config=cfg)
    assert result.verdict == "COMMENT"
    assert "Looks good overall" in result.summary


# ── registry/spec wiring ──────────────────────────────────────────────────


def test_spec_is_registered_and_marked_local_subprocess() -> None:
    spec = handoff_reviewer.get_spec("pr_agent")
    assert spec.invocation == "local_subprocess"
    assert spec.runner is pr_agent_backend.run
    assert "pr_agent" in handoff_reviewer.known_backends()


def test_pr_agent_in_default_enabled_backends() -> None:
    cfg = PRReviewerConfig()
    assert "pr_agent" in cfg.enabled_backends
    # Stubs must NOT be on by default.
    assert "coderabbit" not in cfg.enabled_backends
    assert "greptile" not in cfg.enabled_backends


def test_dispatch_rejects_local_subprocess_backends() -> None:
    """``dispatch`` is comment-trigger only; pr_agent should be rejected here."""
    # The agent layer routes local_subprocess backends through a separate
    # path, so calling dispatch() with one is a programming error and
    # should raise via _resolve.
    from caretaker.pr_reviewer.handoff_reviewer import _resolve

    cfg = PRReviewerConfig()
    with pytest.raises(ValueError, match="comment-trigger"):
        _resolve("pr_agent", cfg)


def test_greptile_runner_raises_not_implemented() -> None:
    """Stub backend must fail loudly so misconfiguration isn't silent."""
    from caretaker.pr_reviewer.backends import greptile

    async def _call() -> None:
        await greptile.run(pr_url="https://github.com/o/r/pull/1")

    with pytest.raises(NotImplementedError, match="greptile"):
        asyncio.run(_call())
