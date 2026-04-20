"""Tests for CausalEventStore ingestion + refresh_from_github (Sprint F3)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from caretaker.admin.causal_store import CausalEventStore
from caretaker.causal import make_causal_marker
from caretaker.causal_chain import CausalEvent, CausalEventRef
from caretaker.github_client.models import (
    Comment,
    Issue,
    Label,
    PRState,
    PullRequest,
    User,
)
from caretaker.state.models import OrchestratorState, TrackedIssue, TrackedPR
from caretaker.state.tracker import TRACKING_ISSUE_TITLE, TRACKING_LABEL


def _user() -> User:
    return User(login="caretaker[bot]", id=42, type="Bot")


def _issue(number: int, body: str, *, title: str = "", labels: list[str] | None = None) -> Issue:
    return Issue(
        number=number,
        title=title,
        body=body,
        user=_user(),
        labels=[Label(name=n) for n in (labels or [])],
        created_at=datetime(2026, 4, 20, tzinfo=UTC),
    )


def _pr(number: int, body: str) -> PullRequest:
    return PullRequest(
        number=number,
        title=f"PR #{number}",
        body=body,
        state=PRState.OPEN,
        user=_user(),
        head_ref="feature",
        base_ref="main",
        mergeable=True,
        merged=False,
        draft=False,
        labels=[],
        created_at=datetime(2026, 4, 20, tzinfo=UTC),
        updated_at=datetime(2026, 4, 20, tzinfo=UTC),
        html_url="",
    )


def _comment(body: str, *, cid: int = 1) -> Comment:
    return Comment(
        id=cid,
        user=_user(),
        body=body,
        created_at=datetime(2026, 4, 20, tzinfo=UTC),
    )


class _FakeGitHubClient:
    """Captures calls; returns fixed Issues/PRs/comments."""

    def __init__(
        self,
        *,
        issues_by_number: dict[int, Issue] | None = None,
        prs_by_number: dict[int, PullRequest] | None = None,
        comments_by_number: dict[int, list[Comment]] | None = None,
        tracking_issues: list[Issue] | None = None,
    ) -> None:
        self._issues_by_number = issues_by_number or {}
        self._prs_by_number = prs_by_number or {}
        self._comments_by_number = comments_by_number or {}
        self._tracking_issues = tracking_issues or []

    async def get_issue(self, owner: str, repo: str, number: int) -> Issue | None:
        return self._issues_by_number.get(number)

    async def get_pull_request(self, owner: str, repo: str, number: int) -> PullRequest | None:
        return self._prs_by_number.get(number)

    async def get_pr_comments(self, owner: str, repo: str, number: int) -> list[Comment]:
        return list(self._comments_by_number.get(number, []))

    async def list_issues(
        self, owner: str, repo: str, *, state: str = "open", labels: str | None = None
    ) -> list[Issue]:
        return list(self._tracking_issues)


class TestIngest:
    def test_ingest_body_adds_event(self) -> None:
        store = CausalEventStore()
        marker = make_causal_marker("devops", run_id=7)
        event = store.ingest_body(
            f"body {marker}",
            ref=CausalEventRef(kind="issue", number=1),
        )
        assert event is not None
        assert store.size() == 1
        assert store.get("run-7-devops") is not None

    def test_ingest_body_returns_none_without_marker(self) -> None:
        store = CausalEventStore()
        assert store.ingest_body("plain body", ref=CausalEventRef(kind="issue")) is None
        assert store.size() == 0

    def test_clear_wipes_index(self) -> None:
        store = CausalEventStore()
        store.ingest(
            CausalEvent(id="x", source="s", parent_id=None, ref=CausalEventRef(kind="issue"))
        )
        store.clear()
        assert store.size() == 0


class TestListAndQueries:
    def _populated(self) -> CausalEventStore:
        store = CausalEventStore()
        store.ingest(
            CausalEvent(
                id="a",
                source="devops",
                parent_id=None,
                ref=CausalEventRef(kind="issue"),
                observed_at=datetime(2026, 4, 1, tzinfo=UTC),
            )
        )
        store.ingest(
            CausalEvent(
                id="b",
                source="issue-agent:dispatch",
                parent_id="a",
                ref=CausalEventRef(kind="issue"),
                observed_at=datetime(2026, 4, 2, tzinfo=UTC),
            )
        )
        return store

    def test_list_returns_newest_first(self) -> None:
        store = self._populated()
        events, total = store.list_events()
        assert total == 2
        assert [e.id for e in events] == ["b", "a"]

    def test_list_filters_by_source(self) -> None:
        store = self._populated()
        events, total = store.list_events(source="devops")
        assert total == 1
        assert events[0].id == "a"

    def test_walk_and_descendants(self) -> None:
        store = self._populated()
        chain = store.walk("b")
        assert [e.id for e in chain.events] == ["a", "b"]
        descs = store.descendants("a")
        assert [e.id for e in descs] == ["b"]


class TestRefreshFromGitHub:
    @pytest.mark.asyncio
    async def test_ingests_tracked_issues_prs_and_tracking_comments(self) -> None:
        parent_marker = make_causal_marker("devops", run_id=42)
        child_marker = make_causal_marker("issue-agent:dispatch", run_id=43, parent="run-42-devops")
        pr_marker = make_causal_marker("pr-agent:escalation", run_id=44)
        tracking_comment_marker = make_causal_marker("state-tracker:run-history", run_id=45)

        tracked_issue = _issue(10, f"Bug body {parent_marker}", title="bug")
        dispatch_comment = _comment(f"dispatch {child_marker}", cid=101)
        tracked_pr = _pr(20, f"PR body {pr_marker}")

        tracking_issue = _issue(
            99,
            "orchestrator state lives here",
            title=TRACKING_ISSUE_TITLE,
            labels=[TRACKING_LABEL],
        )
        tracking_comment = _comment(f"history {tracking_comment_marker}", cid=500)

        github = _FakeGitHubClient(
            issues_by_number={10: tracked_issue},
            prs_by_number={20: tracked_pr},
            comments_by_number={
                10: [dispatch_comment],
                20: [],
                99: [tracking_comment],
            },
            tracking_issues=[tracking_issue],
        )

        state = OrchestratorState(
            tracked_issues={10: TrackedIssue(number=10)},
            tracked_prs={20: TrackedPR(number=20)},
        )

        store = CausalEventStore()
        count = await store.refresh_from_github(github, "o", "r", state)  # type: ignore[arg-type]

        assert count == 4
        assert store.get("run-42-devops") is not None
        assert store.get("run-43-issue-agent:dispatch") is not None
        assert store.get("run-44-pr-agent:escalation") is not None
        assert store.get("run-45-state-tracker:run-history") is not None

        # Chain walks from child to parent.
        chain = store.walk("run-43-issue-agent:dispatch")
        assert [e.id for e in chain.events] == [
            "run-42-devops",
            "run-43-issue-agent:dispatch",
        ]

    @pytest.mark.asyncio
    async def test_clears_previous_events(self) -> None:
        store = CausalEventStore()
        store.ingest(
            CausalEvent(id="stale", source="x", parent_id=None, ref=CausalEventRef(kind="issue"))
        )
        github = _FakeGitHubClient()
        state = OrchestratorState()
        count = await store.refresh_from_github(github, "o", "r", state)  # type: ignore[arg-type]
        assert count == 0
        assert store.get("stale") is None

    @pytest.mark.asyncio
    async def test_skips_non_tracking_issue_titles(self) -> None:
        # Tracking-label issue but wrong title → comments should NOT be scanned.
        unrelated_marker = make_causal_marker("devops", run_id=7)
        bad_tracking = _issue(
            50,
            "body",
            title="not-the-tracking-issue",
            labels=[TRACKING_LABEL],
        )
        github = _FakeGitHubClient(
            comments_by_number={50: [_comment(f"body {unrelated_marker}")]},
            tracking_issues=[bad_tracking],
        )
        state = OrchestratorState()
        store = CausalEventStore()
        count = await store.refresh_from_github(github, "o", "r", state)  # type: ignore[arg-type]
        assert count == 0
