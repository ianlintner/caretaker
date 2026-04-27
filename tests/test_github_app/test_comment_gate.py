"""Tests for the pre-dispatch comment gate."""

from __future__ import annotations

import pytest

from caretaker.github_app.comment_gate import (
    CommentGateMode,
    evaluate_comment_gate,
)
from caretaker.github_app.webhooks import ParsedWebhook


def _parsed(
    *,
    event: str = "issue_comment",
    body: str | None = None,
    actor: str = "alice",
    action: str = "created",
) -> ParsedWebhook:
    """Build a minimal ParsedWebhook with a comment payload."""
    payload: dict[str, object] = {
        "action": action,
        "sender": {"login": actor},
    }
    if body is not None:
        # ``pull_request_review`` carries the body under ``review`` and
        # the others under ``comment`` — synthesise both shapes here so
        # this helper works for every comment-event type the gate
        # inspects.
        if event == "pull_request_review":
            payload["review"] = {"body": body, "user": {"login": actor}}
        else:
            payload["comment"] = {"body": body, "user": {"login": actor}}
    return ParsedWebhook(
        event_type=event,
        delivery_id="00000000-0000-0000-0000-000000000001",
        action=action,
        installation_id=1,
        repository_full_name="ianlintner/caretaker",
        payload=payload,
    )


# ── CommentGateMode parsing ────────────────────────────────────────────


def test_mode_parse_known_values() -> None:
    assert CommentGateMode.parse("off") is CommentGateMode.OFF
    assert CommentGateMode.parse("advise") is CommentGateMode.ADVISE
    assert CommentGateMode.parse("enforce") is CommentGateMode.ENFORCE


def test_mode_parse_defaults_to_advise() -> None:
    # Empty / None / unknown → ADVISE so a fresh deploy gets self-echo
    # safety without the stricter dispatch-cut.
    assert CommentGateMode.parse(None) is CommentGateMode.ADVISE
    assert CommentGateMode.parse("") is CommentGateMode.ADVISE
    assert CommentGateMode.parse("strict") is CommentGateMode.ADVISE


# ── Non-comment events bypass the gate entirely ────────────────────────


@pytest.mark.parametrize(
    "event",
    ["pull_request", "push", "check_run", "workflow_run", "ping"],
)
def test_non_comment_events_proceed_in_every_mode(event: str) -> None:
    parsed = _parsed(event=event, body="@caretaker take this over")
    for mode in CommentGateMode:
        decision = evaluate_comment_gate(parsed, mode=mode)
        assert decision.skip is False
        assert decision.outcome == "proceed"
        assert decision.verdict is None


# ── off mode ──────────────────────────────────────────────────────────


def test_off_mode_proceeds_even_for_self_echo() -> None:
    parsed = _parsed(
        body="caretaker says hi <!-- caretaker:review-result -->",
        actor="caretaker[bot]",
    )
    decision = evaluate_comment_gate(parsed, mode=CommentGateMode.OFF)
    assert decision.skip is False
    assert decision.outcome == "proceed"


# ── self-echo skip (advise + enforce) ─────────────────────────────────


@pytest.mark.parametrize(
    "mode",
    [CommentGateMode.ADVISE, CommentGateMode.ENFORCE],
)
def test_self_echo_skipped(mode: CommentGateMode) -> None:
    """Bot actor + caretaker marker → skip with outcome=self_echo."""
    parsed = _parsed(
        body=("Caretaker review summary: looks good\n<!-- caretaker:review-result -->"),
        actor="caretaker[bot]",
    )
    decision = evaluate_comment_gate(parsed, mode=mode)
    assert decision.skip is True
    assert decision.outcome == "self_echo"
    assert decision.verdict is not None
    assert decision.verdict.is_self_echo is True


# ── human-intent path ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "trigger",
    [
        "@caretaker take this over",
        "@the-care-taker please look",
        "/caretaker help",
        "/maintain ASAP",
        "Could you take a look @CareTaker",  # case-insensitive
    ],
)
def test_human_intent_recognised_in_advise(trigger: str) -> None:
    parsed = _parsed(body=trigger, actor="alice")
    decision = evaluate_comment_gate(parsed, mode=CommentGateMode.ADVISE)
    assert decision.skip is False
    assert decision.outcome == "human_intent"
    assert decision.verdict is not None
    assert decision.verdict.is_human_intent is True


def test_human_intent_recognised_in_enforce() -> None:
    parsed = _parsed(body="@caretaker take this over", actor="alice")
    decision = evaluate_comment_gate(parsed, mode=CommentGateMode.ENFORCE)
    assert decision.skip is False
    assert decision.outcome == "human_intent"


# ── no-intent (plain comment) ─────────────────────────────────────────


def test_no_intent_advise_proceeds() -> None:
    """Plain PR comment in advise mode → unchanged behaviour."""
    parsed = _parsed(body="LGTM, merging soon", actor="alice")
    decision = evaluate_comment_gate(parsed, mode=CommentGateMode.ADVISE)
    assert decision.skip is False
    assert decision.outcome == "proceed"


def test_no_intent_enforce_skipped() -> None:
    """Plain PR comment in enforce mode → drop, outcome=no_human_intent."""
    parsed = _parsed(body="LGTM, merging soon", actor="alice")
    decision = evaluate_comment_gate(parsed, mode=CommentGateMode.ENFORCE)
    assert decision.skip is True
    assert decision.outcome == "no_human_intent"


# ── pull_request_review carries body under "review" ───────────────────


def test_pull_request_review_body_extraction() -> None:
    """Reviews put the body under ``review`` instead of ``comment``."""
    parsed = _parsed(
        event="pull_request_review",
        body="@caretaker please address this",
        actor="alice",
        action="submitted",
    )
    decision = evaluate_comment_gate(parsed, mode=CommentGateMode.ADVISE)
    assert decision.skip is False
    assert decision.outcome == "human_intent"


# ── env var read path (production default) ────────────────────────────


def test_env_var_read_when_mode_omitted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Production callers omit ``mode`` and the helper reads the env var."""
    monkeypatch.setenv("CARETAKER_WEBHOOK_COMMENT_GATING", "enforce")
    parsed = _parsed(body="LGTM", actor="alice")
    decision = evaluate_comment_gate(parsed)
    assert decision.skip is True
    assert decision.outcome == "no_human_intent"


def test_env_var_default_is_advise(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CARETAKER_WEBHOOK_COMMENT_GATING", raising=False)
    parsed = _parsed(body="LGTM", actor="alice")
    decision = evaluate_comment_gate(parsed)
    # advise + no-intent → proceed (current behaviour preserved).
    assert decision.skip is False
    assert decision.outcome == "proceed"


# ── missing payload fields fall through cleanly ───────────────────────


def test_missing_comment_object_treated_as_no_signal() -> None:
    """A malformed payload with no ``comment``/``review`` returns proceed
    in advise mode — no body means no trigger and no marker, so the
    legacy verdict is ``no_self_echo / no_human_intent``.
    """
    parsed = ParsedWebhook(
        event_type="issue_comment",
        delivery_id="00000000-0000-0000-0000-000000000001",
        action="created",
        installation_id=1,
        repository_full_name="ianlintner/caretaker",
        payload={"action": "created"},
    )
    decision = evaluate_comment_gate(parsed, mode=CommentGateMode.ADVISE)
    assert decision.skip is False
    assert decision.outcome == "proceed"
