"""Claude API adapter for enhanced decision-making."""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


class ClaudeClient:
    """Optional Claude integration for premium analysis features."""

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        self._client = None

    @property
    def available(self) -> bool:
        return bool(self._api_key)

    def _ensure_client(self) -> None:
        if self._client is None and self._api_key:
            import anthropic

            self._client = anthropic.Anthropic(api_key=self._api_key)

    async def analyze_ci_logs(self, logs: str, context: str = "") -> str:
        """Analyze CI failure logs and return structured diagnosis."""
        if not self.available:
            return ""
        self._ensure_client()
        assert self._client is not None

        response = self._client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=2000,
            messages=[
                {
                    "role": "user",
                    "content": (
                        "Analyze this CI failure log and provide:\n"
                        "1. Root cause (one line)\n"
                        "2. Affected files and lines\n"
                        "3. Suggested fix (specific code changes)\n\n"
                        f"Context: {context}\n\n"
                        f"CI Log:\n```\n{logs[:8000]}\n```"
                    ),
                }
            ],
        )
        return response.content[0].text

    async def analyze_review_comment(self, comment: str, diff: str) -> str:
        """Analyze a review comment and determine if it's actionable."""
        if not self.available:
            return ""
        self._ensure_client()
        assert self._client is not None

        response = self._client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1000,
            messages=[
                {
                    "role": "user",
                    "content": (
                        "Classify this code review comment:\n\n"
                        f"Comment: {comment}\n\n"
                        f"Diff context:\n```\n{diff[:4000]}\n```\n\n"
                        "Respond with:\n"
                        "CLASSIFICATION: ACTIONABLE | NITPICK | QUESTION | PRAISE\n"
                        "SUMMARY: one-line description of what's needed\n"
                        "COMPLEXITY: trivial | moderate | complex"
                    ),
                }
            ],
        )
        return response.content[0].text

    async def decompose_issue(self, issue_body: str, repo_context: str = "") -> str:
        """Break a large issue into smaller implementable tasks."""
        if not self.available:
            return ""
        self._ensure_client()
        assert self._client is not None

        response = self._client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=3000,
            messages=[
                {
                    "role": "user",
                    "content": (
                        "Break this issue into smaller, implementable sub-issues.\n"
                        "Each should be a focused PR-sized task.\n\n"
                        f"Repository context: {repo_context}\n\n"
                        f"Issue:\n{issue_body}\n\n"
                        "For each sub-issue provide:\n"
                        "- Title\n"
                        "- Description\n"
                        "- Acceptance criteria\n"
                        "- Files likely involved\n"
                        "- Dependencies on other sub-issues"
                    ),
                }
            ],
        )
        return response.content[0].text
