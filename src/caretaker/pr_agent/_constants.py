"""Shared constants for the PR agent."""

from __future__ import annotations

# Logins of well-known automated reviewer bots whose COMMENTED reviews carry
# actionable feedback that should be forwarded to Copilot for remediation.
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
    """Return True if *login* belongs to a known automated reviewer bot."""
    return login in AUTOMATED_REVIEWER_BOTS or login.endswith("[bot]")
