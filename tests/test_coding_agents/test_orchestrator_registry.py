"""Tests for ``Orchestrator._build_coding_agent_registry``.

The interesting behaviours are name validation and the typed-block
priority over the open ``executor.agents`` map.
"""

from __future__ import annotations

import pytest

from caretaker.config import (
    ClaudeCodeExecutorConfig,
    ExecutorConfig,
    HandoffAgentConfig,
    OpenCodeExecutorConfig,
)
from caretaker.orchestrator import Orchestrator


def _build(executor_cfg: ExecutorConfig):
    """Stub-friendly registry builder — the helper only needs strings."""
    return Orchestrator._build_coding_agent_registry(
        github=None,  # type: ignore[arg-type] — HandoffAgent stores it but we never call .run()
        owner="o",
        repo="r",
        executor_cfg=executor_cfg,
    )


def test_typed_blocks_register_when_enabled() -> None:
    cfg = ExecutorConfig(
        claude_code=ClaudeCodeExecutorConfig(enabled=True),
        opencode=OpenCodeExecutorConfig(enabled=True),
    )
    registry = _build(cfg)
    assert registry.has("claude_code")
    assert registry.has("opencode")


def test_extra_agent_registers_with_handoff_mode() -> None:
    cfg = ExecutorConfig(
        agents={"hermes": HandoffAgentConfig(enabled=True, mode="handoff")},
    )
    registry = _build(cfg)
    assert registry.has("hermes")
    agent = registry.get("hermes")
    assert agent is not None
    assert agent.name == "hermes"
    # Marker is interpolated from the validated name.
    assert agent.marker == "<!-- caretaker:hermes-handoff -->"  # type: ignore[attr-defined]


def test_extra_agent_skipped_when_disabled() -> None:
    cfg = ExecutorConfig(
        agents={"hermes": HandoffAgentConfig(enabled=False, mode="handoff")},
    )
    registry = _build(cfg)
    assert not registry.has("hermes")


def test_extra_agent_skipped_for_non_handoff_mode(caplog: pytest.LogCaptureFixture) -> None:
    """Phase 1 only ships ``handoff``; ``inline`` / ``k8s_job`` are reserved."""
    cfg = ExecutorConfig(
        agents={"future_agent": HandoffAgentConfig(enabled=True, mode="inline")},
    )
    with caplog.at_level("WARNING"):
        registry = _build(cfg)
    assert not registry.has("future_agent")
    assert any("not yet supported" in rec.message for rec in caplog.records)


def test_typed_block_wins_over_open_agents_map(caplog: pytest.LogCaptureFixture) -> None:
    """Operator pasted ``opencode`` into both blocks — typed wins."""
    cfg = ExecutorConfig(
        opencode=OpenCodeExecutorConfig(enabled=True, trigger_label="opencode-typed"),
        agents={"opencode": HandoffAgentConfig(enabled=True, trigger_label="opencode-loose")},
    )
    with caplog.at_level("WARNING"):
        registry = _build(cfg)
    agent = registry.get("opencode")
    assert agent is not None
    # Typed block is the one that survived.
    assert agent.trigger_label == "opencode-typed"  # type: ignore[attr-defined]
    assert any("duplicates a typed config block" in rec.message for rec in caplog.records)


@pytest.mark.parametrize(
    "bad_name",
    [
        "Bad-Name",  # uppercase rejected
        "1agent",  # leading digit rejected
        "agent name",  # whitespace rejected
        "agent>",  # HTML metacharacter rejected
        "--agent",  # leading dash rejected
        "",  # empty rejected
    ],
)
def test_invalid_custom_agent_name_skipped(bad_name: str, caplog: pytest.LogCaptureFixture) -> None:
    """Names that would produce malformed markers / labels are rejected.

    The pattern is ``^[a-z][a-z0-9_-]*$`` — anything else either breaks
    the HTML-comment marker (``<!-- caretaker:<name>-handoff -->``) or
    produces a GitHub label that gets rejected at apply time.
    """
    cfg = ExecutorConfig(
        agents={bad_name: HandoffAgentConfig(enabled=True, mode="handoff")},
    )
    with caplog.at_level("WARNING"):
        registry = _build(cfg)
    assert not registry.has(bad_name)
    assert any("not a valid agent name" in rec.message for rec in caplog.records)


@pytest.mark.parametrize("good_name", ["hermes", "codex", "agent-1", "my_agent", "a"])
def test_valid_custom_agent_name_registers(good_name: str) -> None:
    cfg = ExecutorConfig(
        agents={good_name: HandoffAgentConfig(enabled=True, mode="handoff")},
    )
    registry = _build(cfg)
    assert registry.has(good_name)
