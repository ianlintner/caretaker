"""Tests for the DevOps agent."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from caretaker.devops_agent.agent import (
    DEVOPS_AGENT_MARKER,
    DevOpsAgent,
    _failure_signature,
)
from caretaker.devops_agent.log_analyzer import FailureSummary


def make_agent() -> DevOpsAgent:
    github = AsyncMock()
    return DevOpsAgent(github=github, owner="o", repo="r")


def _make_issue_with_sig(sig: str) -> MagicMock:
    """Build a mock issue whose body contains a properly formatted marker."""
    body = f"{DEVOPS_AGENT_MARKER} sig:{sig} -->\n\nSome issue body."
    issue = MagicMock()
    issue.body = body
    return issue


@pytest.mark.asyncio
async def test_get_existing_failure_signatures_strips_sig_prefix() -> None:
    """Extracted signatures must match _failure_signature output (no 'sig:' prefix)."""
    summary = FailureSummary(
        job_name="lint",
        conclusion="failure",
        category="lint",
        suspected_files=[],
        error_lines=[],
    )
    sig = _failure_signature(summary)

    agent = make_agent()
    agent._issues.list = AsyncMock(return_value=[_make_issue_with_sig(sig)])  # type: ignore[attr-defined]

    existing = await agent._get_existing_failure_signatures()
    assert sig in existing, f"Expected {sig!r} in existing signatures {existing!r}"


@pytest.mark.asyncio
async def test_get_existing_failure_signatures_empty_when_no_marker() -> None:
    """Issues without the agent marker contribute no signatures."""
    issue = MagicMock()
    issue.body = "Some unrelated issue body."

    agent = make_agent()
    agent._issues.list = AsyncMock(return_value=[issue])  # type: ignore[attr-defined]

    existing = await agent._get_existing_failure_signatures()
    assert existing == set()
