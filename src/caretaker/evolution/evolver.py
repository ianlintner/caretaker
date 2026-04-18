"""AgentFileEvolver — promotes learned skills to Copilot agent instruction files.

When a skill crosses the promotion threshold (success_count >= 10,
confidence >= 0.80, not already present in the agent file), this evolver
opens a PR to update the relevant `.github/agents/maintainer-*.md` file.

Deferred until InsightStore has >= 100 skills and >= 60 days of data.
Always opens a PR for human review — never auto-merges agent files.

This is a Phase 7 stub.  The scaffolding is in place; the GitHub API calls
for reading agent files and opening PRs will be implemented once the skill
library has enough data to make promotion meaningful.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from caretaker.evolution.insight_store import InsightStore, Skill
    from caretaker.github_client.api import GitHubClient

logger = logging.getLogger(__name__)

PROMOTION_MIN_SUCCESS = 10
PROMOTION_MIN_CONFIDENCE = 0.80
SKILL_LIBRARY_MIN_SIZE = 100


@dataclass
class PromotionCandidate:
    skill: Skill
    agent_file: str  # e.g. "maintainer-pr.md"
    suggested_text: str


class AgentFileEvolver:
    """Promotes high-confidence skills to Copilot agent instruction files."""

    def __init__(
        self,
        github: GitHubClient,
        owner: str,
        repo: str,
        insight_store: InsightStore,
    ) -> None:
        self._github = github
        self._owner = owner
        self._repo = repo
        self._store = insight_store

    def find_promotable(self) -> list[PromotionCandidate]:
        """Return skills eligible for promotion to agent files."""
        all_skills = self._store.all_skills()
        total = len(all_skills)

        if total < SKILL_LIBRARY_MIN_SIZE:
            logger.debug(
                "AgentFileEvolver: skill library too small (%d < %d), skipping promotion check",
                total,
                SKILL_LIBRARY_MIN_SIZE,
            )
            return []

        candidates: list[PromotionCandidate] = []
        for skill in all_skills:
            if skill.success_count < PROMOTION_MIN_SUCCESS:
                continue
            if skill.confidence < PROMOTION_MIN_CONFIDENCE:
                continue
            agent_file = self._skill_to_agent_file(skill)
            candidates.append(
                PromotionCandidate(
                    skill=skill,
                    agent_file=agent_file,
                    suggested_text=self._format_hint(skill),
                )
            )
        return candidates

    async def propose_updates(self) -> list[int]:
        """Open PRs for promotable skills.  Returns list of created PR numbers."""
        candidates = self.find_promotable()
        if not candidates:
            return []

        logger.info("AgentFileEvolver: %d promotion candidates found", len(candidates))
        # Phase 7 stub — full implementation deferred until skill library matures
        return []

    def _skill_to_agent_file(self, skill: Skill) -> str:
        mapping = {
            "ci": "maintainer-pr.md",
            "build": "maintainer-pr.md",
            "issue": "maintainer-issue.md",
            "security": "maintainer-security.md",
        }
        return mapping.get(skill.category, "maintainer-pr.md")

    def _format_hint(self, skill: Skill) -> str:
        return (
            f"## Learned pattern: {skill.signature[:60]}\n\n"
            f"{skill.sop_text}\n\n"
            f"_(Confidence: {skill.confidence:.0%}, {skill.success_count} verified fixes)_"
        )
