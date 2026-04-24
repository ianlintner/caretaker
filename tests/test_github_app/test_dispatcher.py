"""Tests for the webhook → agent dispatcher (Phase 2)."""

from __future__ import annotations

import asyncio

import pytest

from caretaker.github_app.dispatcher import (
    DispatchMode,
    WebhookDispatcher,
    dispatch_in_background,
)
from caretaker.github_app.webhooks import ParsedWebhook


def _make_parsed(
    *,
    event: str = "pull_request",
    delivery: str = "00000000-0000-0000-0000-000000000001",
    action: str | None = "opened",
    installation_id: int | None = 42,
    repository_full_name: str | None = "ianlintner/space-tycoon",
) -> ParsedWebhook:
    return ParsedWebhook(
        event_type=event,
        delivery_id=delivery,
        action=action,
        installation_id=installation_id,
        repository_full_name=repository_full_name,
        payload={"action": action},
    )


# ── DispatchMode.parse ───────────────────────────────────────────────


def test_parse_mode_handles_known_values() -> None:
    assert DispatchMode.parse("off") is DispatchMode.OFF
    assert DispatchMode.parse("shadow") is DispatchMode.SHADOW
    assert DispatchMode.parse("active") is DispatchMode.ACTIVE


def test_parse_mode_is_case_and_whitespace_insensitive() -> None:
    assert DispatchMode.parse(" Shadow ") is DispatchMode.SHADOW
    assert DispatchMode.parse("ACTIVE") is DispatchMode.ACTIVE


def test_parse_mode_defaults_to_off_for_none_or_empty() -> None:
    assert DispatchMode.parse(None) is DispatchMode.OFF
    assert DispatchMode.parse("") is DispatchMode.OFF


def test_parse_mode_defaults_to_off_for_unknown_values() -> None:
    # Unknown modes should never execute — silently downgrading to OFF
    # is safer than raising, since this runs at webhook-handler time.
    assert DispatchMode.parse("turbo") is DispatchMode.OFF


# ── off mode ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_off_mode_does_not_run_agents() -> None:
    dispatcher = WebhookDispatcher(mode=DispatchMode.OFF)
    result = await dispatcher.dispatch(_make_parsed())

    assert result.mode is DispatchMode.OFF
    assert result.outcome == "off"
    # Agents are still resolved (for the result envelope + logging), but
    # no execution / shadow work happens.
    assert result.agents == ("pr", "pr-reviewer")
    assert result.duration_seconds >= 0.0


# ── shadow mode ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_shadow_mode_reports_agents_that_would_run() -> None:
    dispatcher = WebhookDispatcher(mode=DispatchMode.SHADOW)
    result = await dispatcher.dispatch(_make_parsed(event="workflow_run"))

    assert result.mode is DispatchMode.SHADOW
    assert result.outcome == "shadow"
    assert set(result.agents) == {"devops", "self-heal", "pr"}
    assert result.detail is not None
    assert "3 agents" in result.detail


@pytest.mark.asyncio
async def test_shadow_mode_on_unrouted_event_reports_no_agents() -> None:
    dispatcher = WebhookDispatcher(mode=DispatchMode.SHADOW)
    result = await dispatcher.dispatch(_make_parsed(event="ping", action=None))

    assert result.outcome == "no_agents"
    assert result.agents == ()


@pytest.mark.asyncio
async def test_shadow_mode_emits_structured_log_per_agent(
    caplog: pytest.LogCaptureFixture,
) -> None:
    dispatcher = WebhookDispatcher(mode=DispatchMode.SHADOW)
    with caplog.at_level("INFO", logger="caretaker.github_app.dispatcher"):
        await dispatcher.dispatch(_make_parsed(event="pull_request"))

    would_run_lines = [r.message for r in caplog.records if "would-dispatch" in r.message]
    # One line per agent registered for pull_request (pr + pr-reviewer).
    assert len(would_run_lines) == 2
    # Delivery id threads through so operators can grep by it.
    assert all("delivery=00000000-0000-0000-0000-000000000001" in m for m in would_run_lines)


# ── active mode ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_active_mode_surfaces_error_outcome_until_wired() -> None:
    """Active mode is intentionally unwired — dispatch catches the
    NotImplementedError and maps it to an ``error`` outcome so a
    premature flip still produces a metric instead of a crash."""
    dispatcher = WebhookDispatcher(mode=DispatchMode.ACTIVE)
    result = await dispatcher.dispatch(_make_parsed())

    assert result.mode is DispatchMode.ACTIVE
    assert result.outcome == "error"
    assert result.detail is not None
    assert "NotImplementedError" in result.detail


# ── dispatch() error isolation ──────────────────────────────────────


@pytest.mark.asyncio
async def test_dispatch_never_raises_even_on_internal_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Webhook dispatch must be crash-proof — GitHub will not tell us
    we dropped an event. Simulate a failure inside the shadow path and
    assert we still return a well-formed ``DispatchResult``."""

    def boom(*_args: object, **_kwargs: object) -> list[str]:
        raise RuntimeError("agent map blew up")

    monkeypatch.setattr("caretaker.github_app.dispatcher.agents_for_event", boom)

    dispatcher = WebhookDispatcher(mode=DispatchMode.SHADOW)
    result = await dispatcher.dispatch(_make_parsed())

    assert result.outcome == "error"
    assert result.detail is not None
    assert "agent map blew up" in result.detail


# ── dispatch_in_background ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_dispatch_in_background_returns_awaitable_task() -> None:
    dispatcher = WebhookDispatcher(mode=DispatchMode.SHADOW)
    task = dispatch_in_background(dispatcher, _make_parsed())

    assert isinstance(task, asyncio.Task)
    # Task is nameable so oncall can spot dispatcher work in
    # asyncio.all_tasks() output.
    assert "webhook-dispatch" in task.get_name()

    result = await task
    assert result.outcome == "shadow"


@pytest.mark.asyncio
async def test_dispatch_in_background_isolates_handler_from_slow_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The webhook handler must be able to schedule a dispatch and
    return immediately, regardless of how long the dispatch itself
    takes. Prove the handler doesn't block by scheduling a task whose
    dispatch is delayed, asserting we get control back before it
    resolves."""

    async def slow_dispatch(_self: WebhookDispatcher, _parsed: ParsedWebhook):  # type: ignore[no-untyped-def]
        await asyncio.sleep(0.1)
        return "done"

    monkeypatch.setattr(WebhookDispatcher, "dispatch", slow_dispatch)

    dispatcher = WebhookDispatcher(mode=DispatchMode.SHADOW)
    task = dispatch_in_background(dispatcher, _make_parsed())

    assert not task.done()  # handler got control back before dispatch ran
    assert await task == "done"
