"""Backward-compat shim — :class:`ClaudeCodeExecutor` lives in ``coding_agents`` now.

The old module-level surface (``ClaudeCodeExecutor`` /
``CLAUDE_CODE_HANDOFF_MARKER``) is preserved so callers that import from
``caretaker.claude_code_executor`` keep working through one deprecation
window. New code should import from :mod:`caretaker.coding_agents`
directly:

    from caretaker.coding_agents import ClaudeCodeAgent

The name change reflects that the executor is one of several BYOCA
hand-off agents (Claude Code, opencode, …) sharing a common base.
"""

from __future__ import annotations

from caretaker.coding_agents.handoff import (
    CLAUDE_CODE_HANDOFF_MARKER,
    ClaudeCodeAgent,
)

# Legacy name kept for one release. Identical to ``ClaudeCodeAgent``.
ClaudeCodeExecutor = ClaudeCodeAgent

__all__ = [
    "CLAUDE_CODE_HANDOFF_MARKER",
    "ClaudeCodeAgent",
    "ClaudeCodeExecutor",
]
