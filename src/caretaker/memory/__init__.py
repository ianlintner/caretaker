"""Cross-cutting memory helpers — M5 of the memory-graph plan.

Per-agent "core memory" (CoALA working-memory scope) lives here. The
per-agent SQLite key-value store is still rooted in
:mod:`caretaker.state.memory`; this package hosts the graph-side
mirror and the shared query helpers layered on top of it.
"""

from __future__ import annotations

from caretaker.memory.core import AgentCoreMemory, publish

__all__ = ["AgentCoreMemory", "publish"]
