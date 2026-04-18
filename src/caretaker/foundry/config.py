"""Re-exports of the foundry executor config types.

The Pydantic models live in :mod:`caretaker.config` alongside the rest of the
agent configs — we re-export here for ergonomic imports inside the foundry
package.
"""

from __future__ import annotations

from caretaker.config import ExecutorConfig, FoundryExecutorConfig

__all__ = ["ExecutorConfig", "FoundryExecutorConfig"]
