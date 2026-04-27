"""BYOCA — Bring Your Own Coding Agent registry.

This package contains the pluggable coding-agent infrastructure caretaker
uses to dispatch coding work (lint fixes, test fixes, review-comment fixes)
and complex PR reviews. Each agent is a concrete implementation of the
:class:`~caretaker.coding_agents.protocol.CodingAgent` protocol.

Two execution modes ship today:

* ``handoff`` — caretaker tags the host PR / issue with a configurable
  trigger label and posts a structured ``@mention`` comment. An upstream
  GitHub Action installed on the consumer repo (``anthropics/claude-code-action``,
  ``sst/opencode-github-action``, etc.) picks the comment up and produces
  the work asynchronously. Caretaker treats the dispatch itself as the
  unit of work.
* ``inline`` — used today only by ``foundry`` (caretaker's own tool-loop
  in :mod:`caretaker.foundry.executor`). Future placeholder for inline
  subprocess agents like ``opencode run``, ``codex …``, ``gemini …``.

The naming ``coding_agents`` is deliberately distinct from the existing
``caretaker.agents`` package, which holds caretaker's *internal* agents
(PR agent, issue agent, etc.) — those orchestrate work; these *do* the
coding work.
"""

from __future__ import annotations

from caretaker.coding_agents.handoff import (
    CLAUDE_CODE_HANDOFF_MARKER,
    OPENCODE_HANDOFF_MARKER,
    ClaudeCodeAgent,
    HandoffAgent,
    OpenCodeAgent,
)
from caretaker.coding_agents.protocol import CodingAgent, ExecutionMode
from caretaker.coding_agents.registry import CodingAgentRegistry

__all__ = [
    "CLAUDE_CODE_HANDOFF_MARKER",
    "OPENCODE_HANDOFF_MARKER",
    "ClaudeCodeAgent",
    "CodingAgent",
    "CodingAgentRegistry",
    "ExecutionMode",
    "HandoffAgent",
    "OpenCodeAgent",
]
