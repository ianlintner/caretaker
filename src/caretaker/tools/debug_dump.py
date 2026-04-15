"""Utilities for rendering structured debug dumps in GitHub markdown."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any


def render_debug_dump(
    payload: dict[str, Any],
    *,
    title: str = "Debug dump",
    max_chars: int = 12000,
) -> str:
    """Render a collapsible JSON debug dump suitable for issue/comment bodies."""
    serialized = json.dumps(payload, indent=2, sort_keys=True, default=str)
    if len(serialized) > max_chars:
        serialized = f"{serialized[:max_chars]}\n... [truncated by caretaker]"

    generated_at = datetime.now(UTC).isoformat()
    return (
        f"\n\n<details>\n"
        f"<summary>{title}</summary>\n\n"
        f"Generated at: `{generated_at}`\n\n"
        "```json\n"
        f"{serialized}\n"
        "```\n"
        "</details>"
    )
