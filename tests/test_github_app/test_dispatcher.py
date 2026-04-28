"""Tests for the webhook → agent dispatcher (Phase 2)."""

from __future__ import annotations

import asyncio

import pytest

from caretaker.github_app.dispatcher import (
    AgentContextFactory,
    AgentRunner,
    DispatchMode,
    WebhookDispatcher,
    dispatch_in_background,
    in_flight_count,
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


class _FakeContext:
    """Stand-in for ``AgentContext`` — the dispatcher treats it as opaque."""


class _FakeFactory:
    """Records build() calls so tests can assert we don't build once per agent."""

    def __init__(self) -> None:
        self.builds: list[ParsedWebhook] = []

    async def build(self, parsed: ParsedWebhook) -> _FakeContext:  # type: ignore[override]
        self.builds.append(parsed)
        return _FakeContext()


class _RecordingRunner:
    """Runner that returns a caller-supplied outcome per agent name."""

    def __init__(self, outcomes: dict[str, str]) -> None:
        self._outcomes = outcomes
        self.calls: list[str] = []

    async def run(
        self,
        *,
        agent_name: str,
        context: _FakeContext,  # type: ignore[override]
        parsed: ParsedWebhook,
    ) -> str:
        self.calls.append(agent_name)
        outcome = self._outcomes.get(agent_name, "success")
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


@pytest.mark.asyncio
async def test_active_mode_without_factory_or_runner_surfaces_error() -> None:
    """Missing collaborators is a misconfiguration — dispatch must record
    an error outcome loudly rather than silently running nothing."""
    dispatcher = WebhookDispatcher(mode=DispatchMode.ACTIVE)
    result = await dispatcher.dispatch(_make_parsed())

    assert result.mode is DispatchMode.ACTIVE
    assert result.outcome == "error"
    assert result.detail is not None
    assert "context_factory" in result.detail


@pytest.mark.asyncio
async def test_active_mode_runs_resolved_agents_and_builds_context_once() -> None:
    factory = _FakeFactory()
    runner = _RecordingRunner(outcomes={"pr": "success", "pr-reviewer": "success"})
    dispatcher = WebhookDispatcher(
        mode=DispatchMode.ACTIVE,
        context_factory=factory,  # type: ignore[arg-type]
        agent_runner=runner,  # type: ignore[arg-type]
    )

    result = await dispatcher.dispatch(_make_parsed())

    assert result.outcome == "active"
    assert result.detail == "active ran=2 failed=0 shadowed=0"
    # Context must be built once per dispatch, not once per agent — it's
    # the installation token / GitHubClient that's expensive to mint.
    assert len(factory.builds) == 1
    # Runner called once per agent, in order.
    assert runner.calls == ["pr", "pr-reviewer"]


@pytest.mark.asyncio
async def test_active_mode_allow_list_shadows_unlisted_agents() -> None:
    factory = _FakeFactory()
    runner = _RecordingRunner(outcomes={"pr-reviewer": "success"})
    dispatcher = WebhookDispatcher(
        mode=DispatchMode.ACTIVE,
        context_factory=factory,  # type: ignore[arg-type]
        agent_runner=runner,  # type: ignore[arg-type]
        active_agents=frozenset({"pr-reviewer"}),
    )

    result = await dispatcher.dispatch(_make_parsed())

    # Only pr-reviewer runs; "pr" is shadowed.
    assert runner.calls == ["pr-reviewer"]
    assert result.outcome == "active"
    assert result.detail == "active ran=1 failed=0 shadowed=1"


@pytest.mark.asyncio
async def test_active_mode_empty_allow_list_shadows_everything() -> None:
    """A rollout starting with zero promoted agents still builds context
    and logs — but never calls the runner."""
    factory = _FakeFactory()
    runner = _RecordingRunner(outcomes={})
    dispatcher = WebhookDispatcher(
        mode=DispatchMode.ACTIVE,
        context_factory=factory,  # type: ignore[arg-type]
        agent_runner=runner,  # type: ignore[arg-type]
        active_agents=frozenset(),
    )

    result = await dispatcher.dispatch(_make_parsed())

    assert runner.calls == []
    assert result.outcome == "active"
    assert result.detail == "active ran=0 failed=0 shadowed=2"


@pytest.mark.asyncio
async def test_active_mode_isolates_one_agent_failure_from_siblings() -> None:
    """One agent raising must not abort the rest of the fan-out."""
    factory = _FakeFactory()
    runner = _RecordingRunner(
        outcomes={
            "pr": RuntimeError("pr agent exploded"),
            "pr-reviewer": "success",
        },
    )
    dispatcher = WebhookDispatcher(
        mode=DispatchMode.ACTIVE,
        context_factory=factory,  # type: ignore[arg-type]
        agent_runner=runner,  # type: ignore[arg-type]
    )

    result = await dispatcher.dispatch(_make_parsed())

    # Both agents were attempted — the second still ran after the first raised.
    assert runner.calls == ["pr", "pr-reviewer"]
    assert result.outcome == "active_partial"
    assert result.detail == "active ran=1 failed=1 shadowed=0"


@pytest.mark.asyncio
async def test_active_mode_per_agent_timeout_maps_to_timeout_outcome() -> None:
    """A slow agent must be bounded by ``agent_timeout_seconds`` and
    surface as ``active_partial`` without hanging the dispatch."""

    class _SlowRunner:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def run(
            self,
            *,
            agent_name: str,
            context: _FakeContext,  # type: ignore[override]
            parsed: ParsedWebhook,
        ) -> str:
            self.calls.append(agent_name)
            await asyncio.sleep(10.0)
            return "success"  # pragma: no cover — timeout fires first

    factory = _FakeFactory()
    runner = _SlowRunner()
    dispatcher = WebhookDispatcher(
        mode=DispatchMode.ACTIVE,
        agent_timeout_seconds=0.05,
        context_factory=factory,  # type: ignore[arg-type]
        agent_runner=runner,  # type: ignore[arg-type]
        active_agents=frozenset({"pr"}),  # keep the fan-out to one agent
    )

    result = await dispatcher.dispatch(_make_parsed())

    assert runner.calls == ["pr"]
    assert result.outcome == "active_partial"
    assert "failed=1" in (result.detail or "")


@pytest.mark.asyncio
async def test_active_mode_factory_failure_records_error_outcome() -> None:
    """If the factory itself raises (e.g. installation token mint
    failed), dispatch must record ``error`` — not partial success."""

    class _BrokenFactory:
        async def build(self, parsed: ParsedWebhook) -> _FakeContext:  # type: ignore[override]
            raise RuntimeError("token broker unreachable")

    runner = _RecordingRunner(outcomes={})
    dispatcher = WebhookDispatcher(
        mode=DispatchMode.ACTIVE,
        context_factory=_BrokenFactory(),  # type: ignore[arg-type]
        agent_runner=runner,  # type: ignore[arg-type]
    )

    result = await dispatcher.dispatch(_make_parsed())

    assert result.outcome == "error"
    assert runner.calls == []
    assert "token broker unreachable" in (result.detail or "")


def test_protocols_are_runtime_importable() -> None:
    """Protocols are exported from the dispatcher module so third-party
    implementations can type-check against them."""
    assert AgentContextFactory is not None
    assert AgentRunner is not None


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

    assert task is not None
    assert not task.done()  # handler got control back before dispatch ran
    assert await task == "done"


# ── in-flight cap ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dispatch_in_background_drops_when_in_flight_cap_reached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once the in-flight cap is hit, further dispatches return ``None``
    and don't grow the in-flight set. This is the memory-pressure relief
    valve: during GitHub rate-limit cooldown, every dispatch waits on
    the cooldown without releasing the parsed payload — without a cap,
    a webhook burst would OOM the pod."""
    monkeypatch.setenv("CARETAKER_WEBHOOK_MAX_IN_FLIGHT", "2")

    # Block dispatch indefinitely so tasks accumulate in the in-flight set.
    gate = asyncio.Event()

    async def blocking_dispatch(_self: WebhookDispatcher, _parsed: ParsedWebhook):  # type: ignore[no-untyped-def]
        await gate.wait()
        return "done"

    monkeypatch.setattr(WebhookDispatcher, "dispatch", blocking_dispatch)

    dispatcher = WebhookDispatcher(mode=DispatchMode.SHADOW)

    # Drain any existing in-flight tasks from prior tests.
    starting = in_flight_count()

    t1 = dispatch_in_background(dispatcher, _make_parsed(delivery="d1"))
    t2 = dispatch_in_background(dispatcher, _make_parsed(delivery="d2"))
    assert t1 is not None
    assert t2 is not None
    assert in_flight_count() == starting + 2

    # Cap is 2 (relative). With ``starting`` already in flight from
    # outside this test, the third call MAY still slip through if the
    # global was empty — so use a unique cap baseline.
    monkeypatch.setenv("CARETAKER_WEBHOOK_MAX_IN_FLIGHT", str(in_flight_count()))
    dropped = dispatch_in_background(dispatcher, _make_parsed(delivery="d3"))
    assert dropped is None  # over the cap → dropped
    assert in_flight_count() == starting + 2  # in-flight set didn't grow

    # Releasing the gate lets the held tasks finish so the in-flight
    # set shrinks back; verifies the done-callback wires up correctly.
    gate.set()
    await asyncio.gather(t1, t2)
    # The done callback runs on the next event-loop tick.
    await asyncio.sleep(0)
    assert in_flight_count() == starting


# ── comment gate (self-echo / human-intent) ──────────────────────────


def _make_comment_parsed(
    *,
    body: str,
    actor: str,
    event: str = "issue_comment",
    delivery: str = "00000000-0000-0000-0000-000000aaaa01",
) -> ParsedWebhook:
    """ParsedWebhook with a realistic ``issue_comment`` payload shape."""
    return ParsedWebhook(
        event_type=event,
        delivery_id=delivery,
        action="created",
        installation_id=42,
        repository_full_name="ianlintner/caretaker",
        payload={
            "action": "created",
            "comment": {"body": body, "user": {"login": actor}},
            "sender": {"login": actor},
        },
    )


@pytest.mark.asyncio
async def test_self_echo_short_circuits_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Caretaker's own bot-authored comment with the marker → skip.

    No agents resolve, no factory build, no runner call. The result
    envelope carries ``outcome="self_echo"`` so operators can see it on
    the metric and structured log.
    """
    monkeypatch.setenv("CARETAKER_WEBHOOK_COMMENT_GATING", "advise")

    factory = _FakeFactory()
    runner = _RecordingRunner(outcomes={})
    dispatcher = WebhookDispatcher(
        mode=DispatchMode.ACTIVE,
        context_factory=factory,  # type: ignore[arg-type]
        agent_runner=runner,  # type: ignore[arg-type]
    )

    parsed = _make_comment_parsed(
        body="Caretaker review: LGTM\n<!-- caretaker:review-result -->",
        actor="caretaker[bot]",
    )
    result = await dispatcher.dispatch(parsed)

    assert result.outcome == "self_echo"
    # Critically: no agent ran, no installation token was minted.
    assert factory.builds == []
    assert runner.calls == []


@pytest.mark.asyncio
async def test_human_intent_proceeds_in_advise_and_emits_signal(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``@caretaker take this over`` from a human → agents still run,
    but a structured ``webhook gate outcome=human_intent`` log line
    fires so operators can confirm the trigger landed."""
    monkeypatch.setenv("CARETAKER_WEBHOOK_COMMENT_GATING", "advise")

    factory = _FakeFactory()
    runner = _RecordingRunner(outcomes={"issue": "success", "pr": "success"})
    dispatcher = WebhookDispatcher(
        mode=DispatchMode.ACTIVE,
        context_factory=factory,  # type: ignore[arg-type]
        agent_runner=runner,  # type: ignore[arg-type]
    )

    parsed = _make_comment_parsed(
        body="@caretaker take this over",
        actor="alice",
    )

    with caplog.at_level("INFO", logger="caretaker.github_app.dispatcher"):
        result = await dispatcher.dispatch(parsed)

    # Underlying dispatch ran agents normally.
    assert result.outcome == "active"
    assert runner.calls == ["issue", "pr"]
    # Plus the gate logged its verdict — operators grep this in production.
    gate_lines = [
        r.message for r in caplog.records if "webhook gate outcome=human_intent" in r.message
    ]
    assert len(gate_lines) == 1


@pytest.mark.asyncio
async def test_advise_mode_no_intent_proceeds_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A regular PR comment (no @caretaker) in advise mode dispatches
    agents exactly as before — the gate is purely additive."""
    monkeypatch.setenv("CARETAKER_WEBHOOK_COMMENT_GATING", "advise")

    factory = _FakeFactory()
    runner = _RecordingRunner(outcomes={"issue": "success", "pr": "success"})
    dispatcher = WebhookDispatcher(
        mode=DispatchMode.ACTIVE,
        context_factory=factory,  # type: ignore[arg-type]
        agent_runner=runner,  # type: ignore[arg-type]
    )

    parsed = _make_comment_parsed(body="LGTM, merging soon", actor="alice")
    result = await dispatcher.dispatch(parsed)

    assert result.outcome == "active"
    assert runner.calls == ["issue", "pr"]


@pytest.mark.asyncio
async def test_enforce_mode_drops_comments_without_explicit_trigger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In enforce mode, plain comments don't dispatch agents at all —
    only ``@caretaker``/``/caretaker`` mentions do."""
    monkeypatch.setenv("CARETAKER_WEBHOOK_COMMENT_GATING", "enforce")

    factory = _FakeFactory()
    runner = _RecordingRunner(outcomes={})
    dispatcher = WebhookDispatcher(
        mode=DispatchMode.ACTIVE,
        context_factory=factory,  # type: ignore[arg-type]
        agent_runner=runner,  # type: ignore[arg-type]
    )

    parsed = _make_comment_parsed(body="LGTM, merging soon", actor="alice")
    result = await dispatcher.dispatch(parsed)

    assert result.outcome == "no_human_intent"
    assert factory.builds == []
    assert runner.calls == []


@pytest.mark.asyncio
async def test_off_mode_disables_gate_entirely(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``CARETAKER_WEBHOOK_COMMENT_GATING=off`` reverts to legacy: even
    self-echoes dispatch agents (which then no-op via their own marker
    checks). Provides an instant rollback knob if the gate misbehaves."""
    monkeypatch.setenv("CARETAKER_WEBHOOK_COMMENT_GATING", "off")

    factory = _FakeFactory()
    runner = _RecordingRunner(outcomes={"issue": "success", "pr": "success"})
    dispatcher = WebhookDispatcher(
        mode=DispatchMode.ACTIVE,
        context_factory=factory,  # type: ignore[arg-type]
        agent_runner=runner,  # type: ignore[arg-type]
    )

    parsed = _make_comment_parsed(
        body="LGTM <!-- caretaker:review-result -->",
        actor="caretaker[bot]",
    )
    result = await dispatcher.dispatch(parsed)

    # off mode: gate is bypassed → agents run as today.
    assert result.outcome == "active"
    assert runner.calls == ["issue", "pr"]


@pytest.mark.asyncio
async def test_non_comment_event_unaffected_by_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``pull_request.opened`` is state-driven; the gate must never
    short-circuit it even in enforce mode."""
    monkeypatch.setenv("CARETAKER_WEBHOOK_COMMENT_GATING", "enforce")

    factory = _FakeFactory()
    runner = _RecordingRunner(outcomes={"pr": "success", "pr-reviewer": "success"})
    dispatcher = WebhookDispatcher(
        mode=DispatchMode.ACTIVE,
        context_factory=factory,  # type: ignore[arg-type]
        agent_runner=runner,  # type: ignore[arg-type]
    )

    result = await dispatcher.dispatch(_make_parsed(event="pull_request"))

    assert result.outcome == "active"
    assert runner.calls == ["pr", "pr-reviewer"]
