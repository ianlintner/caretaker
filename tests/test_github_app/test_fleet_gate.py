"""Tests for the webhook fleet allow-list (FleetGateConfig).

The fleet gate filters incoming webhooks against
``fleet_gate.allowed_repos`` so caretaker stops generating heartbeats /
observability noise for forks and inactive subscribers. These tests
cover the ``_repo_matches`` helper plus the dispatcher integration
paths (filtered, allowed, owner-wildcard, log toggle).
"""

from __future__ import annotations

import logging

import pytest

from caretaker.github_app.dispatcher import (
    DispatchMode,
    WebhookDispatcher,
    _repo_matches,
)
from caretaker.github_app.webhooks import ParsedWebhook


def _make_parsed(
    *,
    event: str = "pull_request",
    delivery: str = "00000000-0000-0000-0000-000000000001",
    action: str | None = "opened",
    installation_id: int | None = 42,
    repository_full_name: str | None = "ianlintner/caretaker",
) -> ParsedWebhook:
    return ParsedWebhook(
        event_type=event,
        delivery_id=delivery,
        action=action,
        installation_id=installation_id,
        repository_full_name=repository_full_name,
        payload={"action": action},
    )


# ── _repo_matches ────────────────────────────────────────────────────


def test_repo_matches_exact() -> None:
    assert _repo_matches("ianlintner/caretaker", ["ianlintner/caretaker"]) is True


def test_repo_matches_owner_wildcard() -> None:
    assert _repo_matches("ianlintner/foo", ["ianlintner/*"]) is True


def test_repo_matches_universal_wildcard() -> None:
    assert _repo_matches("anyone/anything", ["*"]) is True


def test_repo_matches_no_match() -> None:
    assert _repo_matches("other/repo", ["ianlintner/*"]) is False


def test_repo_matches_empty_patterns_returns_false() -> None:
    assert _repo_matches("ianlintner/caretaker", []) is False


def test_repo_matches_case_insensitive() -> None:
    assert _repo_matches("Ian/Repo", ["ian/repo"]) is True


def test_repo_matches_case_insensitive_owner_wildcard() -> None:
    assert _repo_matches("Ian/Anything", ["ian/*"]) is True


def test_repo_matches_skips_blank_patterns() -> None:
    # Defensive: an operator pasting "owner/foo, ,owner/bar" via env-var
    # split produces blank entries — they must not match anything.
    assert _repo_matches("ianlintner/caretaker", ["", "  "]) is False


def test_repo_matches_owner_wildcard_does_not_match_other_owner() -> None:
    assert _repo_matches("other/foo", ["ianlintner/*"]) is False


def test_repo_matches_handles_extra_whitespace_in_patterns() -> None:
    assert _repo_matches("ianlintner/caretaker", ["  ianlintner/caretaker  "]) is True


def test_repo_matches_owner_wildcard_rejects_bare_slug() -> None:
    """``"owner/*"`` must require a non-empty repo segment after the slash."""
    assert _repo_matches("ianlintner/", ["ianlintner/*"]) is False


def test_repo_matches_owner_wildcard_rejects_prefix_only_match() -> None:
    """``"ianlintner/*"`` must NOT match ``"ianlintnerx/foo"``."""
    assert _repo_matches("ianlintnerx/foo", ["ianlintner/*"]) is False


# ── dispatcher integration ───────────────────────────────────────────


class _FakeContext:
    """Stand-in for ``AgentContext`` — opaque to the dispatcher."""


class _FakeFactory:
    def __init__(self) -> None:
        self.builds: list[ParsedWebhook] = []

    async def build(self, parsed: ParsedWebhook) -> _FakeContext:  # type: ignore[override]
        self.builds.append(parsed)
        return _FakeContext()


class _RecordingRunner:
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
        return "success"


@pytest.mark.asyncio
async def test_dispatcher_allows_all_when_allowlist_empty() -> None:
    """Backward compatibility: empty allow-list → every repo passes."""
    dispatcher = WebhookDispatcher(mode=DispatchMode.SHADOW)
    result = await dispatcher.dispatch(
        _make_parsed(repository_full_name="some/random-fork"),
    )

    assert result.outcome == "shadow"
    assert result.outcome != "not_in_allowlist"


@pytest.mark.asyncio
async def test_dispatcher_filters_repo_not_in_allowlist() -> None:
    """A webhook for an unlisted repo short-circuits before agent
    resolution: the runner / context factory are never touched."""
    factory = _FakeFactory()
    runner = _RecordingRunner()
    dispatcher = WebhookDispatcher(
        mode=DispatchMode.ACTIVE,
        context_factory=factory,  # type: ignore[arg-type]
        agent_runner=runner,  # type: ignore[arg-type]
        allowed_repos=["ianlintner/caretaker"],
    )

    result = await dispatcher.dispatch(
        _make_parsed(repository_full_name="ianlintner/forked-repo"),
    )

    assert result.outcome == "not_in_allowlist"
    assert result.agents == ()
    assert factory.builds == []
    assert runner.calls == []
    assert result.detail is not None
    assert "fleet_gate.allowed_repos" in result.detail


@pytest.mark.asyncio
async def test_dispatcher_passes_repo_in_allowlist() -> None:
    """An allow-listed repo dispatches normally — gate is transparent."""
    factory = _FakeFactory()
    runner = _RecordingRunner()
    dispatcher = WebhookDispatcher(
        mode=DispatchMode.ACTIVE,
        context_factory=factory,  # type: ignore[arg-type]
        agent_runner=runner,  # type: ignore[arg-type]
        allowed_repos=["ianlintner/caretaker"],
    )

    result = await dispatcher.dispatch(
        _make_parsed(repository_full_name="ianlintner/caretaker"),
    )

    assert result.outcome == "active"
    assert len(factory.builds) == 1
    # pull_request event resolves to ("pr", "pr-reviewer") — both run.
    assert runner.calls == ["pr", "pr-reviewer"]


@pytest.mark.asyncio
async def test_dispatcher_owner_wildcard_passes() -> None:
    """``ianlintner/*`` lets every repo under the owner through."""
    factory = _FakeFactory()
    runner = _RecordingRunner()
    dispatcher = WebhookDispatcher(
        mode=DispatchMode.ACTIVE,
        context_factory=factory,  # type: ignore[arg-type]
        agent_runner=runner,  # type: ignore[arg-type]
        allowed_repos=["ianlintner/*"],
    )

    result = await dispatcher.dispatch(
        _make_parsed(repository_full_name="ianlintner/audio-engineer"),
    )

    assert result.outcome == "active"
    assert len(factory.builds) == 1
    assert runner.calls == ["pr", "pr-reviewer"]


@pytest.mark.asyncio
async def test_dispatcher_owner_wildcard_filters_other_owners() -> None:
    """``ianlintner/*`` rejects repos owned by anyone else."""
    factory = _FakeFactory()
    runner = _RecordingRunner()
    dispatcher = WebhookDispatcher(
        mode=DispatchMode.ACTIVE,
        context_factory=factory,  # type: ignore[arg-type]
        agent_runner=runner,  # type: ignore[arg-type]
        allowed_repos=["ianlintner/*"],
    )

    result = await dispatcher.dispatch(
        _make_parsed(repository_full_name="someone-else/random"),
    )

    assert result.outcome == "not_in_allowlist"
    assert factory.builds == []
    assert runner.calls == []


@pytest.mark.asyncio
async def test_dispatcher_log_filtered_emits_log_line(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``log_filtered=True`` emits one INFO line per filtered delivery."""
    dispatcher = WebhookDispatcher(
        mode=DispatchMode.SHADOW,
        allowed_repos=["ianlintner/caretaker"],
        log_filtered=True,
    )

    with caplog.at_level(logging.INFO, logger="caretaker.github_app.dispatcher"):
        result = await dispatcher.dispatch(
            _make_parsed(repository_full_name="ianlintner/forked-repo"),
        )

    assert result.outcome == "not_in_allowlist"
    fleet_lines = [r.message for r in caplog.records if "webhook fleet-gate" in r.message]
    assert len(fleet_lines) == 1
    assert "ianlintner/forked-repo" in fleet_lines[0]


@pytest.mark.asyncio
async def test_dispatcher_log_filtered_quiet_when_false(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``log_filtered=False`` (default) emits no log line on filter."""
    dispatcher = WebhookDispatcher(
        mode=DispatchMode.SHADOW,
        allowed_repos=["ianlintner/caretaker"],
        log_filtered=False,
    )

    with caplog.at_level(logging.INFO, logger="caretaker.github_app.dispatcher"):
        result = await dispatcher.dispatch(
            _make_parsed(repository_full_name="ianlintner/forked-repo"),
        )

    assert result.outcome == "not_in_allowlist"
    fleet_lines = [r.message for r in caplog.records if "webhook fleet-gate" in r.message]
    assert fleet_lines == []


@pytest.mark.asyncio
async def test_dispatcher_filters_when_repo_full_name_missing() -> None:
    """A webhook with no repository slug at all (e.g. installation
    events) is treated as not-in-allowlist when a gate is configured —
    the gate is opt-out at the call site, not opt-in."""
    factory = _FakeFactory()
    runner = _RecordingRunner()
    dispatcher = WebhookDispatcher(
        mode=DispatchMode.ACTIVE,
        context_factory=factory,  # type: ignore[arg-type]
        agent_runner=runner,  # type: ignore[arg-type]
        allowed_repos=["ianlintner/caretaker"],
    )

    result = await dispatcher.dispatch(
        _make_parsed(repository_full_name=None),
    )

    assert result.outcome == "not_in_allowlist"
    assert factory.builds == []
    assert runner.calls == []


@pytest.mark.asyncio
async def test_dispatcher_universal_wildcard_lets_everything_through() -> None:
    """``["*"]`` is the explicit ``allow all`` form — equivalent to an
    empty list, but lets operators be explicit about intent."""
    factory = _FakeFactory()
    runner = _RecordingRunner()
    dispatcher = WebhookDispatcher(
        mode=DispatchMode.ACTIVE,
        context_factory=factory,  # type: ignore[arg-type]
        agent_runner=runner,  # type: ignore[arg-type]
        allowed_repos=["*"],
    )

    result = await dispatcher.dispatch(
        _make_parsed(repository_full_name="some/random-fork"),
    )

    assert result.outcome == "active"
    assert len(factory.builds) == 1
