"""Tests for :mod:`caretaker.identity` — deterministic allowlist + LLM fallback."""

from __future__ import annotations

from typing import Any

import pytest

from caretaker.identity import (
    BotIdentity,
    classify_identity,
    deterministic_family,
    is_automated,
)
from caretaker.identity.bot import _reset_cache_for_tests


@pytest.fixture(autouse=True)
def _clear_identity_cache() -> None:
    """Drop any cached LLM verdicts between tests."""
    _reset_cache_for_tests()


# ── Deterministic allowlist ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "login",
    [
        "copilot",
        "copilot[bot]",
        "copilot-swe-agent",
        "copilot-swe-agent[bot]",
        "github-copilot[bot]",
        "copilot-pull-request-reviewer",
        "dependabot",
        "dependabot[bot]",
        "dependabot-preview[bot]",
        "github-actions[bot]",
        "the-care-taker[bot]",
        "renovate[bot]",
        "github-advanced-security[bot]",
        "coderabbitai[bot]",
        "reviewdog[bot]",
        "sonarcloud[bot]",
        "some-random-suffixed[bot]",
    ],
)
def test_is_automated_known_bots(login: str) -> None:
    assert is_automated(login) is True


@pytest.mark.parametrize(
    "login",
    [
        "alice",
        "octocat",
        "bob-smith",
        "my-org",
        "copilot-but-not",  # no [bot] suffix, not in named allowlist
    ],
)
def test_is_automated_humans(login: str) -> None:
    assert is_automated(login) is False


def test_is_automated_empty_string_is_false() -> None:
    # Goldfish-brain: empty string is NOT a bot.
    assert is_automated("") is False


def test_is_automated_none_raises_typeerror() -> None:
    with pytest.raises(TypeError):
        is_automated(None)  # type: ignore[arg-type]


# ── deterministic_family ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "login, family",
    [
        ("copilot", "copilot"),
        ("dependabot[bot]", "dependabot"),
        ("the-care-taker[bot]", "caretaker"),
        ("github-actions[bot]", "github_bot"),
        ("unknown-suffix[bot]", "github_bot"),
        ("alice", None),
        ("", None),
    ],
)
def test_deterministic_family(login: str, family: str | None) -> None:
    assert deterministic_family(login) == family


def test_deterministic_family_none_raises_typeerror() -> None:
    with pytest.raises(TypeError):
        deterministic_family(None)  # type: ignore[arg-type]


# ── classify_identity deterministic path ────────────────────────────────────


@pytest.mark.asyncio
async def test_classify_identity_known_bot() -> None:
    got = await classify_identity("dependabot[bot]")
    assert got == BotIdentity(is_automated=True, family="dependabot", confidence=1.0)


@pytest.mark.asyncio
async def test_classify_identity_suffix_only() -> None:
    got = await classify_identity("some-weird-bot[bot]")
    assert got.is_automated is True
    assert got.family == "github_bot"
    assert got.confidence == 1.0


@pytest.mark.asyncio
async def test_classify_identity_empty_string() -> None:
    got = await classify_identity("")
    assert got == BotIdentity(is_automated=False, family="human", confidence=1.0)


@pytest.mark.asyncio
async def test_classify_identity_none_raises() -> None:
    with pytest.raises(TypeError):
        await classify_identity(None)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_classify_identity_without_llm_returns_human_fallback() -> None:
    # No LLM client supplied — unfamiliar login maps to "human" with the
    # deterministic confidence.
    got = await classify_identity("alice", llm=None, llm_lookup_enabled=True)
    assert got == BotIdentity(is_automated=False, family="human", confidence=0.9)


@pytest.mark.asyncio
async def test_classify_identity_flag_off_skips_llm() -> None:
    class BoomClient:
        available = True

        async def structured_complete(self, *args: Any, **kwargs: Any) -> BotIdentity:
            raise AssertionError("LLM must not be called when flag is off")

    got = await classify_identity("alice", llm=BoomClient(), llm_lookup_enabled=False)
    assert got.is_automated is False
    assert got.family == "human"


# ── classify_identity LLM fallback ──────────────────────────────────────────


class _StubClaude:
    """Minimal ClaudeClient test double.

    Counts calls so tests can assert memoisation. ``verdict`` or ``error``
    control the simulated response.
    """

    def __init__(
        self,
        verdict: BotIdentity | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self.verdict = verdict
        self.error = error
        self.available = True
        self.call_count = 0

    async def structured_complete(
        self, prompt: str, *, schema: type[BotIdentity], feature: str
    ) -> BotIdentity:
        self.call_count += 1
        if self.error is not None:
            raise self.error
        assert self.verdict is not None
        return self.verdict


@pytest.mark.asyncio
async def test_classify_identity_llm_fallback_and_memoises() -> None:
    llm = _StubClaude(verdict=BotIdentity(is_automated=True, family="github_bot", confidence=0.9))

    first = await classify_identity("mystery-bot", llm=llm, llm_lookup_enabled=True)
    assert first.is_automated is True
    assert first.family == "github_bot"
    assert llm.call_count == 1

    # Second call should hit the cache, not the LLM.
    second = await classify_identity("mystery-bot", llm=llm, llm_lookup_enabled=True)
    assert second == first
    assert llm.call_count == 1


@pytest.mark.asyncio
async def test_classify_identity_llm_error_returns_human_and_does_not_cache() -> None:
    boom = _StubClaude(error=RuntimeError("provider outage"))

    first = await classify_identity("another-mystery", llm=boom, llm_lookup_enabled=True)
    assert first == BotIdentity(is_automated=False, family="human", confidence=0.9)
    assert boom.call_count == 1

    # A follow-up call must invoke the LLM again — errors are *not* memoised.
    second = await classify_identity("another-mystery", llm=boom, llm_lookup_enabled=True)
    assert second == first
    assert boom.call_count == 2


@pytest.mark.asyncio
async def test_classify_identity_llm_unavailable_returns_human() -> None:
    class UnavailableClient:
        available = False

        async def structured_complete(
            self, *args: Any, **kwargs: Any
        ) -> BotIdentity:  # pragma: no cover
            raise AssertionError("must not be called when unavailable")

    got = await classify_identity("some-login", llm=UnavailableClient(), llm_lookup_enabled=True)
    assert got == BotIdentity(is_automated=False, family="human", confidence=0.9)


@pytest.mark.asyncio
async def test_classify_identity_deterministic_short_circuits_before_llm() -> None:
    # Known deterministic login must not consult the LLM even when one is
    # supplied and the flag is on.
    class BoomClient:
        available = True

        async def structured_complete(
            self, *args: Any, **kwargs: Any
        ) -> BotIdentity:  # pragma: no cover
            raise AssertionError("LLM must not be called for known bots")

    got = await classify_identity("copilot[bot]", llm=BoomClient(), llm_lookup_enabled=True)
    assert got == BotIdentity(is_automated=True, family="copilot", confidence=1.0)


# ── Regression: migrated call-sites still classify known logins ─────────────


def test_pr_agent_constants_backcompat() -> None:
    from caretaker.pr_agent._constants import (
        AUTOMATED_REVIEWER_BOTS,
        is_automated_reviewer,
    )

    # Deprecated shims still work and agree with the new API.
    assert is_automated_reviewer("coderabbitai[bot]") is True
    assert is_automated_reviewer("someone") is False
    assert "sonarcloud[bot]" in AUTOMATED_REVIEWER_BOTS


def test_github_models_is_copilot_login_uses_identity() -> None:
    from caretaker.github_client.models import is_copilot_login

    assert is_copilot_login("copilot") is True
    assert is_copilot_login("copilot[bot]") is True
    assert is_copilot_login("copilot-swe-agent[bot]") is True
    assert is_copilot_login("alice") is False
    assert is_copilot_login("dependabot[bot]") is False


def test_pull_request_is_dependabot_pr() -> None:
    from caretaker.github_client.models import PRState, PullRequest, User

    dependabot = PullRequest(
        number=1,
        title="Bump",
        state=PRState.OPEN,
        user=User(login="dependabot[bot]", id=1, type="Bot"),
    )
    preview = PullRequest(
        number=2,
        title="Bump",
        state=PRState.OPEN,
        user=User(login="dependabot-preview[bot]", id=2, type="Bot"),
    )
    human = PullRequest(
        number=3,
        title="Feat",
        state=PRState.OPEN,
        user=User(login="alice", id=3, type="User"),
    )
    assert dependabot.is_dependabot_pr is True
    assert preview.is_dependabot_pr is True
    assert human.is_dependabot_pr is False


# ── Config wiring ───────────────────────────────────────────────────────────


def test_llm_config_exposes_bot_identity_defaults() -> None:
    from caretaker.config import AgenticBotIdentityConfig, LLMConfig

    cfg = LLMConfig()
    assert isinstance(cfg.bot_identity, AgenticBotIdentityConfig)
    assert cfg.bot_identity.llm_lookup_enabled is False
    assert cfg.bot_identity.llm_ttl_seconds == 86_400
    assert cfg.bot_identity.llm_cache_max_size == 1_000
