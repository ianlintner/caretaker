"""Backward-compat shim — the generic dispatcher lives in ``handoff_reviewer``.

Existing call sites that use ``await claude_code_reviewer.dispatch(...)``
keep working through one deprecation window. New code should call
:func:`caretaker.pr_reviewer.handoff_reviewer.dispatch` directly with an
explicit ``backend`` argument so the same path serves Claude Code,
opencode, and any future hand-off reviewer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from caretaker.pr_reviewer.handoff_reviewer import (
    CLAUDE_CODE_REVIEW_MARKER as _HANDOFF_MARKER,
)
from caretaker.pr_reviewer.handoff_reviewer import (
    dispatch as _generic_dispatch,
)

if TYPE_CHECKING:
    from caretaker.config import PRReviewerConfig
    from caretaker.github_client.api import GitHubClient


async def dispatch(
    *,
    github: GitHubClient,
    owner: str,
    repo: str,
    pr_number: int,
    config: PRReviewerConfig,
    routing_reason: str,
) -> bool:
    """Pin to the ``claude_code`` backend; preserved for legacy callers."""
    return await _generic_dispatch(
        backend="claude_code",
        github=github,
        owner=owner,
        repo=repo,
        pr_number=pr_number,
        config=config,
        routing_reason=routing_reason,
    )


__all__ = ["_HANDOFF_MARKER", "dispatch"]
