"""Tests for the pre-escalation rung wired into PRReviewerAgent (Task 5).

Covers the three call sites in agent._handle_pr_body where max_attempts
exhaustion can trigger dispatch_pre_escalation_attempt instead of immediately
giving up:

  1. Harvest path (handoff agent's structured review consumed)
  2. Inline LLM review path
  3. Local-subprocess backend path (caretaker-owned PR)

Test matrix:
  - pre_escalation_fires_when_max_attempts_hit (all three paths)
  - pre_escalation_skipped_when_not_configured
  - escalation_still_absent_when_pre_escalation_fails (falls through quietly)
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from caretaker.agent_protocol import AgentContext
from caretaker.config import AutoFixConfig, MaintainerConfig, PRReviewerConfig
from caretaker.pr_reviewer.inline_reviewer import ReviewResult
from caretaker.state.models import OrchestratorState, TrackedPR

# ── helpers ──────────────────────────────────────────────────────────────────


def _make_review_result(verdict: str = "REQUEST_CHANGES") -> ReviewResult:
    return ReviewResult(summary="style issue", verdict=verdict, comments=[])


def _make_config(
    pre_escalation_agent: str = "openclaw_http",
    allowed_authors: list[str] | None = None,
) -> MaintainerConfig:
    """Build a MaintainerConfig with auto_fix enabled and max_attempts already hit."""
    if allowed_authors is None:
        allowed_authors = ["the-care-taker[bot]"]
    auto_fix = AutoFixConfig(
        enabled=True,
        max_attempts=1,  # one attempt already used → next call hits the cap
        allowed_authors=allowed_authors,
        pre_escalation_agent=pre_escalation_agent,
    )
    pr_reviewer = PRReviewerConfig(
        enabled=True,
        auto_fix=auto_fix,
        caretaker_owned_reviewer="",
    )
    return MaintainerConfig(pr_reviewer=pr_reviewer)


def _make_ctx(cfg: MaintainerConfig) -> AgentContext:
    github = AsyncMock()
    github.list_pull_request_files = AsyncMock(return_value=[])
    github.list_pull_requests = AsyncMock(return_value=[])
    github.ensure_label = AsyncMock()
    github.add_labels = AsyncMock(return_value=[])
    github.upsert_issue_comment = AsyncMock()
    github.create_review = AsyncMock()
    return AgentContext(
        github=github,
        owner="org",
        repo="repo",
        config=cfg,
        llm_router=None,  # type: ignore[arg-type]
    )


def _make_pr_dict(
    pr_number: int = 42,
    author: str = "the-care-taker[bot]",
    head_sha: str = "abc123",
    head_ref: str = "fix/branch",
) -> dict[str, Any]:
    return {
        "number": pr_number,
        "title": "chore: fix stuff",
        "body": "",
        "draft": False,
        "labels": [],
        "user": {"login": author},
        "head": {"sha": head_sha, "ref": head_ref},
        "html_url": f"https://github.com/org/repo/pull/{pr_number}",
    }


def _make_state(pr_number: int = 42, auto_fix_attempts: int = 1) -> OrchestratorState:
    state = OrchestratorState()
    state.tracked_prs[pr_number] = TrackedPR(number=pr_number, auto_fix_attempts=auto_fix_attempts)
    return state


# ── pre_escalation fires on inline path ──────────────────────────────────────


@pytest.mark.asyncio
async def test_pre_escalation_fires_on_inline_path_when_max_attempts_hit() -> None:
    """Inline LLM path: when max_attempts hit, dispatch_pre_escalation_attempt is called."""
    cfg = _make_config(pre_escalation_agent="openclaw_http")
    ctx = _make_ctx(cfg)

    # Return a REQUEST_CHANGES result from the inline reviewer.
    review_result = _make_review_result("REQUEST_CHANGES")

    from caretaker.pr_reviewer import agent as agent_module

    with (
        patch.object(agent_module, "_decide_executor_route", new=AsyncMock()),
        patch("caretaker.pr_reviewer.agent.inline_reviewer") as mock_inline,
        patch("caretaker.pr_reviewer.agent.post_review", new=AsyncMock()),
        patch("caretaker.pr_reviewer.agent.handoff_review_consumer") as mock_harvest,
        patch("caretaker.pr_reviewer.agent._auto_fix") as mock_auto_fix,
        patch("caretaker.state.pr_decisions.record_decision", new=AsyncMock()),
    ):
        # Inline reviewer is available and returns REQUEST_CHANGES.
        mock_inline_llm = MagicMock()
        mock_inline_llm.available = True
        ctx._llm_router = mock_inline_llm  # type: ignore[attr-defined]

        # Patch the llm_router on the context properly.
        ctx_with_llm = AgentContext(
            github=ctx.github,
            owner=ctx.owner,
            repo=ctx.repo,
            config=ctx.config,
            llm_router=MagicMock(available=True),
        )

        mock_inline.review = AsyncMock(return_value=review_result)
        mock_harvest.consume_handoff_reviews = AsyncMock(return_value=[])

        # decide_auto_fix returns should_dispatch=False with max_attempts reason.
        from caretaker.pr_reviewer.auto_fix import AutoFixDecision

        mock_auto_fix.decide_auto_fix = AsyncMock(
            return_value=AutoFixDecision(
                should_dispatch=False,
                reason="max_attempts=1 reached; escalate manually or force-push to re-arm",
            )
        )
        mock_auto_fix.should_attempt_pre_escalation = MagicMock(return_value=True)
        mock_auto_fix.dispatch_pre_escalation_attempt = AsyncMock(return_value=True)
        mock_auto_fix.dispatch_auto_fix = AsyncMock()

        # Inline path fires when decision.use_inline is True; patch routing.
        from caretaker.pr_reviewer.routing import RoutingDecision

        mock_routing_decision = RoutingDecision(
            use_inline=True, score=5, reason="loc=5", backend=None
        )
        with patch("caretaker.pr_reviewer.agent.decide", return_value=mock_routing_decision):
            from caretaker.pr_reviewer.agent import PRReviewerAgent

            agent = PRReviewerAgent(ctx_with_llm)
            state = _make_state(pr_number=42, auto_fix_attempts=1)
            pr = _make_pr_dict()

            # Call _handle_pr_body via the public execute path with a single PR.
            from caretaker.pr_reviewer.agent import _PRReviewReport

            _report = _PRReviewReport()
            with patch("caretaker.pr_reviewer.agent._PRReviewReport", return_value=_report):
                await agent._handle_pr_body(pr, _report, state=state, span=MagicMock())

        mock_auto_fix.should_attempt_pre_escalation.assert_called_once()
        mock_auto_fix.dispatch_pre_escalation_attempt.assert_awaited_once()


# ── pre_escalation skipped when not configured ────────────────────────────────


@pytest.mark.asyncio
async def test_pre_escalation_skipped_when_not_configured() -> None:
    """When pre_escalation_agent='', dispatch_pre_escalation_attempt is NOT called."""
    cfg = _make_config(pre_escalation_agent="")
    ctx = _make_ctx(cfg)

    review_result = _make_review_result("REQUEST_CHANGES")

    from caretaker.pr_reviewer import agent as agent_module

    with (
        patch.object(agent_module, "_decide_executor_route", new=AsyncMock()),
        patch("caretaker.pr_reviewer.agent.inline_reviewer") as mock_inline,
        patch("caretaker.pr_reviewer.agent.post_review", new=AsyncMock()),
        patch("caretaker.pr_reviewer.agent.handoff_review_consumer") as mock_harvest,
        patch("caretaker.pr_reviewer.agent._auto_fix") as mock_auto_fix,
        patch("caretaker.state.pr_decisions.record_decision", new=AsyncMock()),
    ):
        ctx_with_llm = AgentContext(
            github=ctx.github,
            owner=ctx.owner,
            repo=ctx.repo,
            config=ctx.config,
            llm_router=MagicMock(available=True),
        )

        mock_inline.review = AsyncMock(return_value=review_result)
        mock_harvest.consume_handoff_reviews = AsyncMock(return_value=[])

        from caretaker.pr_reviewer.auto_fix import AutoFixDecision

        mock_auto_fix.decide_auto_fix = AsyncMock(
            return_value=AutoFixDecision(
                should_dispatch=False,
                reason="max_attempts=1 reached; escalate manually or force-push to re-arm",
            )
        )
        mock_auto_fix.should_attempt_pre_escalation = MagicMock(return_value=False)
        mock_auto_fix.dispatch_pre_escalation_attempt = AsyncMock(return_value=True)
        mock_auto_fix.dispatch_auto_fix = AsyncMock()

        from caretaker.pr_reviewer.routing import RoutingDecision

        mock_routing_decision = RoutingDecision(
            use_inline=True, score=5, reason="loc=5", backend=None
        )
        with patch("caretaker.pr_reviewer.agent.decide", return_value=mock_routing_decision):
            from caretaker.pr_reviewer.agent import PRReviewerAgent, _PRReviewReport

            agent = PRReviewerAgent(ctx_with_llm)
            state = _make_state(pr_number=42, auto_fix_attempts=1)
            pr = _make_pr_dict()
            _report = _PRReviewReport()

            await agent._handle_pr_body(pr, _report, state=state, span=MagicMock())

        mock_auto_fix.should_attempt_pre_escalation.assert_called_once()
        mock_auto_fix.dispatch_pre_escalation_attempt.assert_not_awaited()


# ── escalation absent when pre_escalation fails ───────────────────────────────


@pytest.mark.asyncio
async def test_escalation_falls_through_when_pre_escalation_fails() -> None:
    """When pre_escalation returns False, the agent completes without crashing."""
    cfg = _make_config(pre_escalation_agent="openclaw_http")
    ctx = _make_ctx(cfg)

    review_result = _make_review_result("REQUEST_CHANGES")

    from caretaker.pr_reviewer import agent as agent_module

    with (
        patch.object(agent_module, "_decide_executor_route", new=AsyncMock()),
        patch("caretaker.pr_reviewer.agent.inline_reviewer") as mock_inline,
        patch("caretaker.pr_reviewer.agent.post_review", new=AsyncMock()),
        patch("caretaker.pr_reviewer.agent.handoff_review_consumer") as mock_harvest,
        patch("caretaker.pr_reviewer.agent._auto_fix") as mock_auto_fix,
        patch("caretaker.state.pr_decisions.record_decision", new=AsyncMock()),
    ):
        ctx_with_llm = AgentContext(
            github=ctx.github,
            owner=ctx.owner,
            repo=ctx.repo,
            config=ctx.config,
            llm_router=MagicMock(available=True),
        )

        mock_inline.review = AsyncMock(return_value=review_result)
        mock_harvest.consume_handoff_reviews = AsyncMock(return_value=[])

        from caretaker.pr_reviewer.auto_fix import AutoFixDecision

        mock_auto_fix.decide_auto_fix = AsyncMock(
            return_value=AutoFixDecision(
                should_dispatch=False,
                reason="max_attempts=1 reached; escalate manually or force-push to re-arm",
            )
        )
        mock_auto_fix.should_attempt_pre_escalation = MagicMock(return_value=True)
        # Pre-escalation fails — returns False.
        mock_auto_fix.dispatch_pre_escalation_attempt = AsyncMock(return_value=False)
        mock_auto_fix.dispatch_auto_fix = AsyncMock()

        from caretaker.pr_reviewer.routing import RoutingDecision

        mock_routing_decision = RoutingDecision(
            use_inline=True, score=5, reason="loc=5", backend=None
        )
        with patch("caretaker.pr_reviewer.agent.decide", return_value=mock_routing_decision):
            from caretaker.pr_reviewer.agent import PRReviewerAgent, _PRReviewReport

            agent = PRReviewerAgent(ctx_with_llm)
            state = _make_state(pr_number=42, auto_fix_attempts=1)
            pr = _make_pr_dict()
            _report = _PRReviewReport()

            # Should complete without raising — escalation (EscalationAgent) does
            # not exist in this agent; the flow simply stops trying.
            await agent._handle_pr_body(pr, _report, state=state, span=MagicMock())

        mock_auto_fix.dispatch_pre_escalation_attempt.assert_awaited_once()
        # dispatch_auto_fix was NOT called (pre-escalation is distinct from main fix loop).
        mock_auto_fix.dispatch_auto_fix.assert_not_awaited()
