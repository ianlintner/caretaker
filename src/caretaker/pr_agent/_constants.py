"""Shared constants for the PR agent.

.. deprecated:: 0.14

    This module is retained for backwards compatibility only. The single
    source of truth for bot-login classification is now
    :mod:`caretaker.identity`. New code should import ``is_automated`` (or
    ``classify_identity`` for richer classification) from there instead of
    using :data:`AUTOMATED_REVIEWER_BOTS` or :func:`is_automated_reviewer`.
"""

from __future__ import annotations

from caretaker.identity import is_automated

# Logins of well-known automated reviewer bots whose COMMENTED reviews carry
# actionable feedback that should be forwarded to Copilot for remediation.
#
# .. deprecated:: 0.14
#    Kept for backwards compatibility. Prefer ``caretaker.identity.is_automated``.
AUTOMATED_REVIEWER_BOTS: frozenset[str] = frozenset(
    {
        "copilot-pull-request-reviewer",
        "github-advanced-security[bot]",
        "coderabbitai[bot]",
        "reviewdog[bot]",
        "sonarcloud[bot]",
    }
)


def is_automated_reviewer(login: str) -> bool:
    """Return True if *login* belongs to a known automated reviewer bot.

    .. deprecated:: 0.14
        Delegates to :func:`caretaker.identity.is_automated`. Prefer the new
        import directly in new code.
    """
    return is_automated(login)
