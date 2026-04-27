"""Tests for :class:`CodingAgentRegistry`."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from caretaker.coding_agents.registry import CodingAgentRegistry

if TYPE_CHECKING:
    import pytest


@dataclass
class _StubAgent:
    name: str
    enabled: bool = True
    mode: str = "handoff"

    async def run(self, task, pr):  # pragma: no cover — never called here
        raise NotImplementedError


def test_register_and_get() -> None:
    reg = CodingAgentRegistry()
    a = _StubAgent(name="claude_code")
    reg.register(a)
    assert reg.get("claude_code") is a
    assert reg.has("claude_code") is True
    assert reg.get("missing") is None
    assert reg.has("missing") is False


def test_names_preserves_insertion_order() -> None:
    reg = CodingAgentRegistry()
    reg.register(_StubAgent(name="claude_code"))
    reg.register(_StubAgent(name="opencode"))
    reg.register(_StubAgent(name="codex"))
    assert reg.names() == ["claude_code", "opencode", "codex"]


def test_enabled_filters_disabled_agents() -> None:
    reg = CodingAgentRegistry()
    reg.register(_StubAgent(name="claude_code", enabled=True))
    reg.register(_StubAgent(name="opencode", enabled=False))
    reg.register(_StubAgent(name="codex", enabled=True))
    assert [a.name for a in reg.enabled()] == ["claude_code", "codex"]


def test_fallback_chain_puts_primary_first() -> None:
    reg = CodingAgentRegistry()
    reg.register(_StubAgent(name="claude_code", enabled=True))
    reg.register(_StubAgent(name="opencode", enabled=True))
    chain = reg.fallback_chain("opencode")
    assert [a.name for a in chain] == ["opencode", "claude_code"]


def test_fallback_chain_skips_disabled_primary() -> None:
    reg = CodingAgentRegistry()
    reg.register(_StubAgent(name="opencode", enabled=False))
    reg.register(_StubAgent(name="claude_code", enabled=True))
    chain = reg.fallback_chain("opencode")
    assert [a.name for a in chain] == ["claude_code"]


def test_register_overwrite_warns(caplog: pytest.LogCaptureFixture) -> None:
    reg = CodingAgentRegistry()
    reg.register(_StubAgent(name="opencode"))
    with caplog.at_level("WARNING"):
        reg.register(_StubAgent(name="opencode"))
    assert any("overwriting existing agent" in rec.message for rec in caplog.records)
