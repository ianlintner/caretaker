"""SkillCrystallizer — writes skills back to InsightStore after verified outcomes.

Called from StateTracker.save() (and optionally from agent code) whenever a
TrackedPR transitions to MERGED or ESCALATED.  The crystallizer extracts the
problem category and signature from the PR's notes field and records the
outcome so the skill library stays current.
"""

from __future__ import annotations

import logging
import re

from caretaker.evolution.insight_store import (
    CATEGORY_BUILD,
    CATEGORY_CI,
    CATEGORY_SECURITY,
    InsightStore,
)
from caretaker.state.models import PRTrackingState, TrackedPR

logger = logging.getLogger(__name__)

# Regex patterns used to infer category from PR notes / CI failure text
_CATEGORY_PATTERNS: list[tuple[str, str]] = [
    (CATEGORY_CI, r"jest|mocha|pytest|vitest|timeout|test|spec"),
    (CATEGORY_BUILD, r"build|webpack|tsc|typescript|compile|import|module"),
    (CATEGORY_SECURITY, r"secret|vuln|cve|dependabot|snyk"),
    (CATEGORY_CI, r"lint|eslint|ruff|flake8|mypy"),
]


def _infer_category(notes: str) -> str:
    """Infer failure category from free-text notes. Defaults to CATEGORY_CI."""
    lower = notes.lower()
    for category, pattern in _CATEGORY_PATTERNS:
        if re.search(pattern, lower):
            return category
    return CATEGORY_CI


def _extract_signature(notes: str) -> str:
    """Extract a normalized signature from PR notes for skill keying.

    Strips PR-specific details (numbers, hashes, timestamps) so the same
    class of problem maps to the same signature across PRs.
    """
    # Remove PR-number-like tokens
    sig = re.sub(r"#\d+", "", notes)
    # Remove hex commit hashes
    sig = re.sub(r"\b[0-9a-f]{7,40}\b", "", sig)
    # Collapse whitespace
    sig = " ".join(sig.split()).strip()
    # Truncate to keep ID stable
    return sig[:120] if sig else "unknown"


class SkillCrystallizer:
    """Records skill outcomes when PRs are resolved.

    Usage — instantiate once per orchestrator run and call
    ``crystallize_transitions()`` before StateTracker.save() so it can compare
    the pre-save state snapshot against the post-agent state.
    """

    def __init__(self, insight_store: InsightStore) -> None:
        self._store = insight_store

    def crystallize_transitions(
        self,
        previous_prs: dict[int, TrackedPR],
        current_prs: dict[int, TrackedPR],
    ) -> int:
        """Compare old vs new PR states and crystallize outcomes.

        Returns the count of skills recorded.
        """
        recorded = 0
        _terminal = {PRTrackingState.MERGED, PRTrackingState.ESCALATED, PRTrackingState.CLOSED}

        for pr_number, current in current_prs.items():
            if current.state not in _terminal:
                continue

            previous = previous_prs.get(pr_number)
            if previous is None:
                previous_state = None
            else:
                previous_state = previous.state

            # Only crystallize on genuine transitions to terminal states
            if previous_state == current.state:
                continue

            notes = current.notes or ""
            if not notes or notes in ("ci_backlog_guard", "closed:ci_backlog_guard"):
                continue

            category = _infer_category(notes)
            signature = _extract_signature(notes)

            if current.state == PRTrackingState.MERGED:
                self._store.record_success(category, signature, sop=notes)
                logger.info(
                    "Crystallized success: PR #%d → category=%s sig='%.40s'",
                    pr_number,
                    category,
                    signature,
                )
                recorded += 1
            elif current.state == PRTrackingState.ESCALATED:
                self._store.record_failure(category, signature)
                logger.debug(
                    "Crystallized failure: PR #%d → category=%s sig='%.40s'",
                    pr_number,
                    category,
                    signature,
                )
                recorded += 1

        return recorded
