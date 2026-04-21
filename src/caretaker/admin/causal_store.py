"""In-memory store of causal events lifted from GitHub bodies/comments.

Populated by :func:`refresh_from_github` — walks open issues + tracked PRs
+ the orchestrator tracking issue's comment stream, pulls every
``<!-- caretaker:causal ... -->`` marker, and indexes by id so the admin
dashboard can walk chains on demand.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from caretaker.causal_chain import (
    CausalEvent,
    CausalEventRef,
    Chain,
    descendants,
    extract_from_body,
    walk_chain,
)
from caretaker.state.tracker import TRACKING_ISSUE_TITLE, TRACKING_LABEL

if TYPE_CHECKING:
    from datetime import datetime

    from caretaker.github_client.api import GitHubClient
    from caretaker.state.models import OrchestratorState

logger = logging.getLogger(__name__)


class CausalEventStore:
    """Indexed collection of :class:`CausalEvent` observed across the repo."""

    def __init__(self) -> None:
        self._index: dict[str, CausalEvent] = {}

    # ── Ingestion ─────────────────────────────────────────────────────────

    def clear(self) -> None:
        self._index = {}

    def ingest(self, event: CausalEvent) -> None:
        """Upsert ``event`` into the index. Later writes win on duplicate id."""
        self._index[event.id] = event

    def ingest_body(
        self,
        body: str,
        *,
        ref: CausalEventRef,
        title: str = "",
        observed_at: datetime | None = None,
    ) -> CausalEvent | None:
        event = extract_from_body(body or "", ref=ref, title=title, observed_at=observed_at)
        if event is not None:
            self.ingest(event)
        return event

    async def refresh_from_github(
        self,
        github: GitHubClient,
        owner: str,
        repo: str,
        state: OrchestratorState,
    ) -> int:
        """Scan tracked PRs/issues + tracking issue comments; return event count."""
        self.clear()

        # Tracked issues — fetch body + comments.
        for number in list(state.tracked_issues.keys()):
            try:
                issue = await github.get_issue(owner, repo, number)
                if issue is not None:
                    self.ingest_body(
                        issue.body or "",
                        ref=CausalEventRef(kind="issue", number=number, owner=owner, repo=repo),
                        title=issue.title or "",
                        observed_at=getattr(issue, "created_at", None),
                    )
                # Comments: reuse get_pr_comments (same endpoint for issues)
                comments = await github.get_pr_comments(owner, repo, number)
                for c in comments:
                    self.ingest_body(
                        c.body or "",
                        ref=CausalEventRef(
                            kind="comment",
                            number=number,
                            comment_id=c.id,
                            owner=owner,
                            repo=repo,
                        ),
                        observed_at=getattr(c, "created_at", None),
                    )
            except Exception:
                logger.debug("Causal refresh: issue #%d skipped", number, exc_info=True)

        # Tracked PRs — body + comments.
        for number in list(state.tracked_prs.keys()):
            try:
                pr = await github.get_pull_request(owner, repo, number)
                if pr is not None:
                    self.ingest_body(
                        pr.body or "",
                        ref=CausalEventRef(kind="pr", number=number, owner=owner, repo=repo),
                        title=pr.title or "",
                        observed_at=getattr(pr, "created_at", None),
                    )
                comments = await github.get_pr_comments(owner, repo, number)
                for c in comments:
                    self.ingest_body(
                        c.body or "",
                        ref=CausalEventRef(
                            kind="comment",
                            number=number,
                            comment_id=c.id,
                            owner=owner,
                            repo=repo,
                        ),
                        observed_at=getattr(c, "created_at", None),
                    )
            except Exception:
                logger.debug("Causal refresh: PR #%d skipped", number, exc_info=True)

        # Tracking issue comments — state-tracker + run-history markers live here.
        try:
            issues = await github.list_issues(owner, repo, state="open", labels=TRACKING_LABEL)
            for tracker_issue in issues:
                if tracker_issue.title != TRACKING_ISSUE_TITLE:
                    continue
                comments = await github.get_pr_comments(owner, repo, tracker_issue.number)
                for c in comments:
                    self.ingest_body(
                        c.body or "",
                        ref=CausalEventRef(
                            kind="comment",
                            number=tracker_issue.number,
                            comment_id=c.id,
                            owner=owner,
                            repo=repo,
                        ),
                        observed_at=getattr(c, "created_at", None),
                    )
        except Exception:
            logger.debug("Causal refresh: tracking issue scan skipped", exc_info=True)

        return len(self._index)

    # ── Queries ───────────────────────────────────────────────────────────

    def size(self) -> int:
        return len(self._index)

    def get(self, event_id: str) -> CausalEvent | None:
        return self._index.get(event_id)

    def index(self) -> dict[str, CausalEvent]:
        return self._index

    def list_events(
        self,
        *,
        source: str | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> tuple[list[CausalEvent], int]:
        events = list(self._index.values())
        if source:
            events = [e for e in events if e.source == source]
        # Most recent first (by observed_at, then by id).
        events.sort(
            key=lambda e: (e.observed_at.isoformat() if e.observed_at else "", e.id),
            reverse=True,
        )
        total = len(events)
        return events[offset : offset + limit], total

    def walk(self, event_id: str, *, max_depth: int = 50) -> Chain:
        return walk_chain(self._index, event_id, max_depth=max_depth)

    def descendants(self, event_id: str, *, max_depth: int = 50) -> list[CausalEvent]:
        return descendants(self._index, event_id, max_depth=max_depth)


__all__ = ["CausalEventStore"]
