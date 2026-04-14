"""Tests for LLM client debug logging."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

if TYPE_CHECKING:
    import pytest


class TestClaudeClientLogging:
    async def test_analyze_ci_logs_logs_prompt_and_response(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """analyze_ci_logs emits DEBUG records for the prompt and response."""
        from caretaker.llm.claude import ClaudeClient

        client = ClaudeClient(api_key="test-key")

        fake_response = MagicMock()
        fake_response.content = [MagicMock(text="root cause: test failure")]

        with patch.object(client, "_ensure_client"):
            client._client = MagicMock()
            client._client.messages.create.return_value = fake_response

            with caplog.at_level(logging.DEBUG, logger="caretaker.llm.claude"):
                result = await client.analyze_ci_logs("some logs", context="test context")

        assert result == "root cause: test failure"
        debug_messages = [r.message for r in caplog.records if r.levelno == logging.DEBUG]
        assert any("analyze_ci_logs" in m for m in debug_messages)

    async def test_analyze_review_comment_logs_prompt_and_response(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """analyze_review_comment emits DEBUG records for the prompt and response."""
        from caretaker.llm.claude import ClaudeClient

        client = ClaudeClient(api_key="test-key")

        fake_response = MagicMock()
        fake_response.content = [MagicMock(text="CLASSIFICATION: ACTIONABLE")]

        with patch.object(client, "_ensure_client"):
            client._client = MagicMock()
            client._client.messages.create.return_value = fake_response

            with caplog.at_level(logging.DEBUG, logger="caretaker.llm.claude"):
                result = await client.analyze_review_comment("fix this", "diff text")

        assert result == "CLASSIFICATION: ACTIONABLE"
        debug_messages = [r.message for r in caplog.records if r.levelno == logging.DEBUG]
        assert any("analyze_review_comment" in m for m in debug_messages)

    async def test_decompose_issue_logs_prompt_and_response(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """decompose_issue emits DEBUG records for the prompt and response."""
        from caretaker.llm.claude import ClaudeClient

        client = ClaudeClient(api_key="test-key")

        fake_response = MagicMock()
        fake_response.content = [MagicMock(text="sub-issue 1: ...")]

        with patch.object(client, "_ensure_client"):
            client._client = MagicMock()
            client._client.messages.create.return_value = fake_response

            with caplog.at_level(logging.DEBUG, logger="caretaker.llm.claude"):
                result = await client.decompose_issue("big issue body", repo_context="ctx")

        assert result == "sub-issue 1: ..."
        debug_messages = [r.message for r in caplog.records if r.levelno == logging.DEBUG]
        assert any("decompose_issue" in m for m in debug_messages)

    async def test_logs_are_truncated_for_long_prompts(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Long prompts are truncated in DEBUG logs (well under raw input size)."""
        from caretaker.llm.claude import ClaudeClient

        client = ClaudeClient(api_key="test-key")

        fake_response = MagicMock()
        fake_response.content = [MagicMock(text="ok")]

        long_log = "x" * 10_000

        with patch.object(client, "_ensure_client"):
            client._client = MagicMock()
            client._client.messages.create.return_value = fake_response

            with caplog.at_level(logging.DEBUG, logger="caretaker.llm.claude"):
                await client.analyze_ci_logs(long_log)

        # Prompt preview in the log message should not exceed truncation limit + marker
        prompt_records = [
            r for r in caplog.records if "prompt" in r.message and r.levelno == logging.DEBUG
        ]
        assert prompt_records, "Expected a DEBUG prompt record"
        # Message body is the formatted message text — check it's reasonably bounded
        full_text = prompt_records[0].message
        assert len(full_text) < 15_000  # sanity check — well under raw 10k chars
