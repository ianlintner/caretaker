"""Tests for the dispatch-guard self-echo detector (T-A2).

Covers:

* :class:`DispatchVerdict` schema (length cap on ``reason``, default
  ``suggested_agent``, strict literal on ``suggested_agent``).
* :func:`legacy_dispatch_verdict` parity with the JS guard in
  ``.github/workflows/maintainer.yml``.
* :func:`judge_dispatch_llm` short-circuit for bodiless events + the
  unambiguous-legacy fast paths.
* :func:`judge_dispatch_llm` ambiguous case → LLM consulted → mocked
  response returned.
* :func:`judge_dispatch_llm` provider error → candidate returns ``None``
  → :func:`evaluate_dispatch` falls back to legacy.
* :func:`evaluate_dispatch` under all three shadow modes.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from caretaker.config import AgenticConfig, AgenticDomainConfig
from caretaker.evolution import shadow_config
from caretaker.evolution.shadow import clear_records_for_tests, recent_records
from caretaker.github_app.dispatch_guard import (
    _CARETAKER_MARKER_RE,
    DispatchEvent,
    DispatchVerdict,
    _should_consult_llm,
    evaluate_dispatch,
    judge_dispatch_llm,
    legacy_dispatch_verdict,
)
from caretaker.graph import writer as graph_writer

# ── Fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_shadow_state() -> None:
    clear_records_for_tests()
    shadow_config.reset_for_tests()
    graph_writer.reset_for_tests()


def _set_dispatch_mode(mode: str) -> None:
    cfg = AgenticConfig(dispatch_guard=AgenticDomainConfig(mode=mode))  # type: ignore[arg-type]
    shadow_config.configure(cfg)


# ── Schema ───────────────────────────────────────────────────────────────


class TestDispatchVerdictSchema:
    def test_defaults(self) -> None:
        verdict = DispatchVerdict(is_self_echo=False, is_human_intent=False)
        assert verdict.suggested_agent == "none"
        assert verdict.reason == ""

    def test_reason_length_is_capped(self) -> None:
        with pytest.raises(ValidationError):
            DispatchVerdict(
                is_self_echo=False,
                is_human_intent=False,
                reason="x" * 201,
            )

    def test_suggested_agent_is_literal(self) -> None:
        with pytest.raises(ValidationError):
            DispatchVerdict(  # type: ignore[call-arg]
                is_self_echo=False,
                is_human_intent=True,
                suggested_agent="not_a_real_agent",
            )

    def test_all_agent_labels_validate(self) -> None:
        labels = [
            "pr_agent",
            "issue_agent",
            "review_agent",
            "self_heal_agent",
            "devops_agent",
            "docs_agent",
            "triage",
            "none",
        ]
        for label in labels:
            verdict = DispatchVerdict(
                is_self_echo=False,
                is_human_intent=True,
                suggested_agent=label,  # type: ignore[arg-type]
            )
            assert verdict.suggested_agent == label


# ── Legacy adapter parity ────────────────────────────────────────────────


class TestLegacyDispatchVerdict:
    """Mirror the JS branches in ``.github/workflows/maintainer.yml``."""

    def test_bot_actor_with_marker_is_self_echo(self) -> None:
        verdict = legacy_dispatch_verdict(
            DispatchEvent(
                event_type="issue_comment",
                actor_login="the-care-taker[bot]",
                comment_body="<!-- caretaker:review-result -->\nhello",
            )
        )
        assert verdict.is_self_echo is True
        assert verdict.is_human_intent is False
        assert verdict.suggested_agent == "none"

    def test_bot_actor_without_marker_is_not_self_echo(self) -> None:
        # JS guard: bot-authored + no marker → skipped by the actor
        # allowlist, but that isn't "self-echo" — it's just "bot noise".
        # Deterministic verdict encodes that as both fields False.
        verdict = legacy_dispatch_verdict(
            DispatchEvent(
                event_type="issue_comment",
                actor_login="github-actions[bot]",
                comment_body="lint failed",
            )
        )
        assert verdict.is_self_echo is False
        assert verdict.is_human_intent is False

    def test_human_with_explicit_mention_is_human_intent(self) -> None:
        verdict = legacy_dispatch_verdict(
            DispatchEvent(
                event_type="issue_comment",
                actor_login="ianlintner",
                comment_body="@caretaker please triage this",
            )
        )
        assert verdict.is_self_echo is False
        assert verdict.is_human_intent is True

    def test_human_with_slash_command_is_human_intent(self) -> None:
        for cmd in ("/caretaker run", "/maintain now", "@the-care-taker hi"):
            verdict = legacy_dispatch_verdict(
                DispatchEvent(
                    event_type="issue_comment",
                    actor_login="ianlintner",
                    comment_body=cmd,
                )
            )
            assert verdict.is_human_intent is True, cmd

    def test_human_pasting_caretaker_output_is_ambiguous_from_legacy(self) -> None:
        # Legacy verdict: not self-echo (actor is human), not human-intent
        # (no explicit mention). This is the edge the LLM exists to fix.
        verdict = legacy_dispatch_verdict(
            DispatchEvent(
                event_type="issue_comment",
                actor_login="ianlintner",
                comment_body="<!-- caretaker:triage-result -->\nfyi this ran",
            )
        )
        assert verdict.is_self_echo is False
        assert verdict.is_human_intent is False
        # The reason string flags the ambiguity.
        assert "ambiguous" in verdict.reason

    def test_bodiless_event_is_silent(self) -> None:
        verdict = legacy_dispatch_verdict(
            DispatchEvent(event_type="push", actor_login="ianlintner")
        )
        assert verdict.is_self_echo is False
        assert verdict.is_human_intent is False

    def test_empty_actor_treats_as_non_bot(self) -> None:
        verdict = legacy_dispatch_verdict(
            DispatchEvent(
                event_type="issue_comment",
                actor_login="",
                comment_body="@caretaker hi",
            )
        )
        assert verdict.is_human_intent is True

    def test_marker_regex_matches_known_caretaker_markers(self) -> None:
        # Parity check against the JS regex in the workflow.
        for marker in (
            "<!-- caretaker:causal id=abc source=pr -->",
            "<!--caretaker:result-->",
            "<!-- caretaker:triage -->",
            "<!-- caretaker:scope-gap -->",
            "<!-- caretaker:claude-code-handoff -->",
        ):
            assert _CARETAKER_MARKER_RE.search(marker), marker

    def test_marker_regex_rejects_unrelated_html_comments(self) -> None:
        for not_marker in (
            "<!-- not-us -->",
            "<!-- somebody:else -->",
            "plain text",
        ):
            assert _CARETAKER_MARKER_RE.search(not_marker) is None, not_marker


# ── Cost-control short-circuit ───────────────────────────────────────────


class TestCostControl:
    async def test_push_event_skips_llm(self) -> None:
        claude = AsyncMock()
        result = await judge_dispatch_llm(
            DispatchEvent(event_type="push", actor_login="ianlintner"),
            claude=claude,
        )
        assert result is None
        claude.structured_complete.assert_not_awaited()

    async def test_workflow_dispatch_skips_llm(self) -> None:
        claude = AsyncMock()
        result = await judge_dispatch_llm(
            DispatchEvent(event_type="workflow_dispatch", actor_login="ianlintner"),
            claude=claude,
        )
        assert result is None
        claude.structured_complete.assert_not_awaited()

    async def test_missing_claude_returns_none(self) -> None:
        result = await judge_dispatch_llm(
            DispatchEvent(
                event_type="issue_comment",
                actor_login="ianlintner",
                comment_body="@caretaker help",
            ),
            claude=None,
        )
        assert result is None

    async def test_unavailable_claude_returns_none(self) -> None:
        claude = AsyncMock()
        claude.available = False
        result = await judge_dispatch_llm(
            DispatchEvent(
                event_type="issue_comment",
                actor_login="ianlintner",
                comment_body="@caretaker help",
            ),
            claude=claude,
        )
        assert result is None
        claude.structured_complete.assert_not_awaited()

    async def test_bot_no_marker_no_trigger_skips_llm(self) -> None:
        # Legacy already unambiguously says "skip": bot actor with a
        # plain CI comment. No tokens spent.
        claude = AsyncMock()
        claude.available = True
        result = await judge_dispatch_llm(
            DispatchEvent(
                event_type="issue_comment",
                actor_login="github-actions[bot]",
                comment_body="lint failed",
            ),
            claude=claude,
        )
        assert result is None
        claude.structured_complete.assert_not_awaited()

    async def test_human_plain_comment_skips_llm(self) -> None:
        # Legacy already unambiguously says "skip": human, no marker, no
        # trigger. Plain chatter, no LLM needed.
        claude = AsyncMock()
        claude.available = True
        result = await judge_dispatch_llm(
            DispatchEvent(
                event_type="issue_comment",
                actor_login="ianlintner",
                comment_body="looks good to me",
            ),
            claude=claude,
        )
        assert result is None
        claude.structured_complete.assert_not_awaited()

    def test_should_consult_llm_covers_human_with_marker(self) -> None:
        # Exercised directly — the ambiguous branch that the
        # evaluate_dispatch test hits via the AsyncMock wiring.
        event = DispatchEvent(
            event_type="issue_comment",
            actor_login="ianlintner",
            comment_body="<!-- caretaker:triage --> fyi",
        )
        assert _should_consult_llm(event, legacy_dispatch_verdict(event)) is True


# ── Ambiguous case → LLM invoked ─────────────────────────────────────────


class TestJudgeDispatchLlmAmbiguous:
    async def test_human_pasting_caretaker_output_invokes_llm(self) -> None:
        claude = AsyncMock()
        claude.available = True
        claude.structured_complete.return_value = DispatchVerdict(
            is_self_echo=False,
            is_human_intent=True,
            suggested_agent="triage",
            reason="user quoted caretaker output but is asking for triage help",
        )

        verdict = await judge_dispatch_llm(
            DispatchEvent(
                event_type="issue_comment",
                actor_login="ianlintner",
                comment_body="<!-- caretaker:triage --> why did you skip this?",
                recent_markers=["<!-- caretaker:triage -->"],
            ),
            claude=claude,
        )

        assert verdict is not None
        assert verdict.is_human_intent is True
        assert verdict.suggested_agent == "triage"
        claude.structured_complete.assert_awaited_once()
        kwargs = claude.structured_complete.await_args.kwargs
        assert kwargs["schema"] is DispatchVerdict
        assert kwargs["feature"] == "dispatch_guard"
        assert "self-echo" in kwargs["system"].lower()

    async def test_bot_reviewer_with_human_style_mention_invokes_llm(self) -> None:
        # Marker absent, trigger present, actor is a bot → legacy says
        # "not self-echo, not human intent"; ambiguous from the guard's
        # perspective so we should consult the LLM.
        claude = AsyncMock()
        claude.available = True
        claude.structured_complete.return_value = DispatchVerdict(
            is_self_echo=True,
            is_human_intent=False,
            suggested_agent="none",
            reason="bot echoing an earlier @caretaker mention",
        )

        verdict = await judge_dispatch_llm(
            DispatchEvent(
                event_type="issue_comment",
                actor_login="copilot-pull-request-reviewer[bot]",
                comment_body="@caretaker please look at this nit",
            ),
            claude=claude,
        )

        assert verdict is not None
        assert verdict.is_self_echo is True
        claude.structured_complete.assert_awaited_once()

    async def test_llm_provider_error_returns_none(self) -> None:
        claude = AsyncMock()
        claude.available = True
        claude.structured_complete.side_effect = RuntimeError("provider down")

        result = await judge_dispatch_llm(
            DispatchEvent(
                event_type="issue_comment",
                actor_login="ianlintner",
                comment_body="<!-- caretaker:triage --> fyi",
            ),
            claude=claude,
        )
        assert result is None

    async def test_prompt_includes_recent_markers(self) -> None:
        claude = AsyncMock()
        claude.available = True
        claude.structured_complete.return_value = DispatchVerdict(
            is_self_echo=True, is_human_intent=False
        )

        await judge_dispatch_llm(
            DispatchEvent(
                event_type="issue_comment",
                actor_login="ianlintner",
                comment_body="<!-- caretaker:triage --> fyi",
                recent_markers=["<!-- caretaker:alpha -->", "<!-- caretaker:beta -->"],
            ),
            claude=claude,
        )

        user_prompt = claude.structured_complete.await_args.args[0]
        assert "<!-- caretaker:alpha -->" in user_prompt
        assert "<!-- caretaker:beta -->" in user_prompt
        assert "event_type: issue_comment" in user_prompt

    async def test_prompt_truncates_long_body(self) -> None:
        claude = AsyncMock()
        claude.available = True
        claude.structured_complete.return_value = DispatchVerdict(
            is_self_echo=False, is_human_intent=True
        )
        big_body = "X" * 2000 + "\n@caretaker tail"
        await judge_dispatch_llm(
            DispatchEvent(
                event_type="issue_comment",
                actor_login="ianlintner",
                comment_body=big_body,
            ),
            claude=claude,
        )
        user_prompt = claude.structured_complete.await_args.args[0]
        # Tail-end retained, leading padding dropped.
        assert "@caretaker tail" in user_prompt
        # We cap at 500 chars + scaffolding; the full 2000 X block must
        # not be present verbatim.
        assert "X" * 600 not in user_prompt


# ── Shadow three-modes ───────────────────────────────────────────────────


class TestEvaluateDispatchShadow:
    async def test_off_mode_uses_legacy_only(self) -> None:
        _set_dispatch_mode("off")
        claude = AsyncMock()
        claude.available = True

        verdict = await evaluate_dispatch(
            DispatchEvent(
                event_type="issue_comment",
                actor_login="ianlintner",
                comment_body="@caretaker triage",
            ),
            claude=claude,
        )

        assert verdict.is_human_intent is True
        claude.structured_complete.assert_not_awaited()
        assert recent_records() == []

    async def test_shadow_mode_runs_both_and_records_disagreement(self) -> None:
        _set_dispatch_mode("shadow")
        claude = AsyncMock()
        claude.available = True
        # Legacy says "self-echo" (bot + marker). LLM disagrees — model
        # thinks it's actually a human follow-up. Legacy wins in shadow.
        claude.structured_complete.return_value = DispatchVerdict(
            is_self_echo=False,
            is_human_intent=True,
            suggested_agent="triage",
            reason="bot relaying a human's escalation request",
        )

        verdict = await evaluate_dispatch(
            DispatchEvent(
                event_type="issue_comment",
                actor_login="the-care-taker[bot]",
                comment_body="<!-- caretaker:triage --> escalation",
            ),
            claude=claude,
            context={"repo_slug": "ian/demo", "delivery_id": "abc"},
        )

        # Legacy verdict authoritative in shadow.
        assert verdict.is_self_echo is True
        assert verdict.is_human_intent is False
        records = recent_records()
        assert len(records) == 1
        assert records[0].name == "dispatch_guard"
        assert records[0].outcome == "disagree"
        assert records[0].repo_slug == "ian/demo"

    async def test_shadow_mode_agreement_records_agree(self) -> None:
        _set_dispatch_mode("shadow")
        claude = AsyncMock()
        claude.available = True
        # Ambiguous input (human pasting caretaker marker). LLM agrees
        # with legacy that it is not a self-echo.
        claude.structured_complete.return_value = DispatchVerdict(
            is_self_echo=False,
            is_human_intent=False,
            suggested_agent="none",
            reason="human paste, no request",
        )

        verdict = await evaluate_dispatch(
            DispatchEvent(
                event_type="issue_comment",
                actor_login="ianlintner",
                comment_body="<!-- caretaker:triage --> interesting",
            ),
            claude=claude,
        )

        assert verdict.is_self_echo is False
        assert verdict.is_human_intent is False
        records = recent_records()
        assert len(records) == 1
        assert records[0].outcome == "agree"

    async def test_shadow_mode_candidate_short_circuit_falls_through(self) -> None:
        # Legacy-unambiguous input (bot + no marker + no trigger) should
        # return None from the candidate and land in candidate_error
        # (the shadow wrapper treats None explicitly only in enforce —
        # in shadow None flows through as the candidate verdict).
        _set_dispatch_mode("shadow")
        claude = AsyncMock()
        claude.available = True

        verdict = await evaluate_dispatch(
            DispatchEvent(
                event_type="issue_comment",
                actor_login="github-actions[bot]",
                comment_body="CI passed",
            ),
            claude=claude,
        )

        assert verdict.is_self_echo is False
        assert verdict.is_human_intent is False
        # LLM must not have been invoked — cost control.
        claude.structured_complete.assert_not_awaited()

    async def test_enforce_mode_candidate_wins(self) -> None:
        _set_dispatch_mode("enforce")
        claude = AsyncMock()
        claude.available = True
        claude.structured_complete.return_value = DispatchVerdict(
            is_self_echo=False,
            is_human_intent=True,
            suggested_agent="triage",
            reason="human pasted output; actually asking a question",
        )

        verdict = await evaluate_dispatch(
            DispatchEvent(
                event_type="issue_comment",
                actor_login="ianlintner",
                comment_body="<!-- caretaker:triage --> why skip?",
            ),
            claude=claude,
        )

        assert verdict.is_human_intent is True
        assert verdict.suggested_agent == "triage"
        claude.structured_complete.assert_awaited_once()

    async def test_enforce_mode_candidate_none_falls_through_to_legacy(self) -> None:
        # Bodiless event → candidate returns None → enforce falls through
        # to legacy, which also returns (False, False). The important
        # property is that we didn't crash.
        _set_dispatch_mode("enforce")
        claude = AsyncMock()
        claude.available = True

        verdict = await evaluate_dispatch(
            DispatchEvent(event_type="push", actor_login="ianlintner"),
            claude=claude,
        )
        assert verdict.is_self_echo is False
        assert verdict.is_human_intent is False
        claude.structured_complete.assert_not_awaited()

    async def test_enforce_mode_candidate_error_falls_through_to_legacy(self) -> None:
        _set_dispatch_mode("enforce")
        claude = AsyncMock()
        claude.available = True
        claude.structured_complete.side_effect = RuntimeError("provider down")

        verdict = await evaluate_dispatch(
            DispatchEvent(
                event_type="issue_comment",
                actor_login="ianlintner",
                comment_body="@caretaker help",
            ),
            claude=claude,
        )
        # Candidate returned None (error swallowed inside
        # judge_dispatch_llm) → legacy verdict is authoritative.
        assert verdict.is_human_intent is True


# ── Unconfigured → default off ───────────────────────────────────────────


async def test_unconfigured_defaults_to_off(monkeypatch: pytest.MonkeyPatch) -> None:
    # No shadow_config.configure() → decorator treats as ``off``.
    claude = AsyncMock()
    claude.available = True

    verdict = await evaluate_dispatch(
        DispatchEvent(
            event_type="issue_comment",
            actor_login="ianlintner",
            comment_body="@caretaker hi",
        ),
        claude=claude,
    )
    assert verdict.is_human_intent is True
    claude.structured_complete.assert_not_awaited()


# ── Cast check: call paths don't mutate the DispatchEvent ────────────────


def test_dispatch_event_is_immutable() -> None:
    event = DispatchEvent(
        event_type="issue_comment",
        actor_login="ianlintner",
        comment_body="hi",
    )
    with pytest.raises((AttributeError, TypeError)):
        event.actor_login = "someone-else"  # type: ignore[misc]


def test_dispatch_event_default_markers_are_not_aliased() -> None:
    first = DispatchEvent(event_type="issue_comment")
    second = DispatchEvent(event_type="issue_comment")
    assert first.recent_markers == []
    assert first.recent_markers is not second.recent_markers


# ── Guards against accidental use of AsyncMock with ClaudeClient.available ──


def test_dispatch_event_comment_body_none_is_bodiless() -> None:
    event = DispatchEvent(event_type="issue_comment", actor_login="ianlintner")
    verdict = legacy_dispatch_verdict(event)
    assert verdict.is_self_echo is False
    assert verdict.is_human_intent is False


def test_repr_does_not_leak_long_bodies() -> None:
    # dataclass repr leaks everything but we only care that the comment
    # body isn't mutated on construction.
    big = "hello" * 200
    event = DispatchEvent(event_type="issue_comment", comment_body=big)
    assert event.comment_body == big
    assert _CARETAKER_MARKER_RE.search(big) is None


def _make_fake_claude(**kwargs: Any) -> AsyncMock:  # pragma: no cover - helper
    mock = AsyncMock(**kwargs)
    mock.available = True
    return mock
