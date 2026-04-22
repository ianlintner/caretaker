"""Tests for the GitHub token scope-gap tracker + issue reporter."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from caretaker.github_client.api import GitHubAPIError, GitHubClient
from caretaker.github_client.models import Issue, Label, User
from caretaker.github_client.scope_gap import (
    ScopeGapTracker,
    get_tracker,
    infer_scope_hint,
    is_scope_gap_message,
    reset_for_tests,
)
from caretaker.github_client.scope_gap_reporter import (
    SCOPE_GAP_ACTION_LABEL,
    SCOPE_GAP_ISSUE_MARKER,
    SCOPE_GAP_ISSUE_TITLE,
    SCOPE_GAP_LABEL,
    publish_scope_gap_issue,
    render_issue_body,
)

# ── Tracker ─────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_scope_tracker() -> None:
    reset_for_tests()


class TestScopeGapTracker:
    def test_empty_tracker_reports_no_incidents(self) -> None:
        tracker = ScopeGapTracker()
        assert tracker.is_empty()
        assert tracker.snapshot() == []

    def test_first_record_creates_incident(self) -> None:
        tracker = ScopeGapTracker()
        incident = tracker.record("GET", "/repos/o/r/dependabot/alerts")
        assert not tracker.is_empty()
        assert incident.count == 1
        assert incident.scope_hint == "security_events: read"
        assert incident.method == "GET"
        assert incident.endpoint == "/repos/o/r/dependabot/alerts"
        assert incident.example_paths == ["/repos/o/r/dependabot/alerts"]

    def test_same_endpoint_and_method_dedupes(self) -> None:
        tracker = ScopeGapTracker()
        tracker.record("GET", "/repos/o/r/dependabot/alerts")
        tracker.record("GET", "/repos/o/r/dependabot/alerts")
        tracker.record("GET", "/repos/o/r/dependabot/alerts")
        snapshot = tracker.snapshot()
        assert len(snapshot) == 1
        assert snapshot[0].count == 3

    def test_numeric_ids_are_templated(self) -> None:
        """Two POSTs to /pulls/5/merge and /pulls/6/merge dedupe to one row."""
        tracker = ScopeGapTracker()
        tracker.record("POST", "/repos/o/r/pulls/5/merge")
        incident = tracker.record("POST", "/repos/o/r/pulls/6/merge")
        snapshot = tracker.snapshot()
        assert len(snapshot) == 1
        assert snapshot[0].count == 2
        assert snapshot[0].endpoint == "/repos/o/r/pulls/:n/merge"
        assert incident.endpoint == "/repos/o/r/pulls/:n/merge"

    def test_different_methods_are_separate_incidents(self) -> None:
        tracker = ScopeGapTracker()
        tracker.record("GET", "/repos/o/r/issues/5")
        tracker.record("PATCH", "/repos/o/r/issues/5")
        snapshot = tracker.snapshot()
        assert {row.method for row in snapshot} == {"GET", "PATCH"}

    def test_example_paths_capped(self) -> None:
        tracker = ScopeGapTracker()
        for n in range(10):
            tracker.record("POST", f"/repos/o/r/pulls/{n}/merge")
        snapshot = tracker.snapshot()
        assert len(snapshot) == 1
        assert len(snapshot[0].example_paths) == ScopeGapTracker._MAX_EXAMPLE_PATHS
        assert snapshot[0].count == 10

    def test_snapshot_is_deterministic(self) -> None:
        tracker = ScopeGapTracker()
        tracker.record("POST", "/repos/o/r/pulls")
        tracker.record("GET", "/repos/o/r/dependabot/alerts")
        tracker.record("GET", "/repos/o/r/secret-scanning/alerts")
        ordered = [(r.scope_hint, r.endpoint, r.method) for r in tracker.snapshot()]
        assert ordered == sorted(ordered)

    def test_reset_clears_all(self) -> None:
        tracker = ScopeGapTracker()
        tracker.record("GET", "/repos/o/r/dependabot/alerts")
        tracker.reset()
        assert tracker.is_empty()

    def test_grouped_by_scope_collapses(self) -> None:
        tracker = ScopeGapTracker()
        tracker.record("GET", "/repos/o/r/dependabot/alerts")
        tracker.record("GET", "/repos/o/r/code-scanning/alerts")
        tracker.record("POST", "/repos/o/r/pulls")
        grouped = tracker.grouped_by_scope()
        assert set(grouped.keys()) == {"security_events: read", "pull_requests: write"}
        assert len(grouped["security_events: read"]) == 2

    def test_global_singleton_shared(self) -> None:
        a = get_tracker()
        b = get_tracker()
        assert a is b


# ── Endpoint → scope map ───────────────────────────────────────────────


class TestInferScopeHint:
    @pytest.mark.parametrize(
        "method,path,expected",
        [
            ("GET", "/repos/o/r/dependabot/alerts", "security_events: read"),
            ("GET", "/repos/o/r/code-scanning/alerts", "security_events: read"),
            ("GET", "/repos/o/r/secret-scanning/alerts", "security_events: read"),
            ("POST", "/repos/o/r/pulls", "pull_requests: write"),
            ("PUT", "/repos/o/r/pulls/5/merge", "pull_requests: write"),
            ("POST", "/repos/o/r/pulls/5/reviews", "pull_requests: write"),
            ("POST", "/repos/o/r/issues/5/assignees", "issues: write"),
            ("POST", "/repos/o/r/issues", "issues: write"),
            ("PATCH", "/repos/o/r/issues/5", "issues: write"),
            ("POST", "/repos/o/r/issues/5/comments", "issues: write"),
            ("POST", "/repos/o/r/check-runs", "checks: write"),
            ("POST", "/repos/o/r/actions/runs/42/rerun", "actions: write"),
            ("PUT", "/repos/o/r/contents/path/to/file", "contents: write"),
        ],
    )
    def test_known_endpoints(self, method: str, path: str, expected: str) -> None:
        assert infer_scope_hint(method, path) == expected

    def test_unknown_endpoint_falls_back(self) -> None:
        hint = infer_scope_hint("GET", "/totally/unknown/endpoint")
        assert hint == "metadata: read"


# ── 403 message detection ─────────────────────────────────────────────


class TestIsScopeGapMessage:
    def test_matches_integration_phrasing(self) -> None:
        assert is_scope_gap_message("Resource not accessible by integration")

    def test_matches_pat_phrasing(self) -> None:
        assert is_scope_gap_message("Resource not accessible by personal access token")

    def test_matches_admin_phrasing(self) -> None:
        assert is_scope_gap_message("Must have admin rights to Repository.")

    def test_rejects_bad_credentials(self) -> None:
        # That's an invalid-token problem, not a scope problem.
        assert not is_scope_gap_message("Bad credentials")

    def test_rejects_rate_limit(self) -> None:
        assert not is_scope_gap_message("API rate limit exceeded for installation.")

    def test_rejects_empty(self) -> None:
        assert not is_scope_gap_message("")


# ── Client 403 → tracker wiring ───────────────────────────────────────


def _make_response(status_code: int, json_body: dict | None = None, text: str = "") -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    resp.headers = {}
    if json_body is not None:
        resp.json.return_value = json_body
        resp.text = str(json_body)
    else:
        resp.json.side_effect = Exception("no json")
    return resp


@pytest.mark.asyncio
async def test_403_scope_gap_records_incident_and_raises() -> None:
    """A 403 with 'Resource not accessible by integration' feeds the tracker."""
    with patch.dict("os.environ", {"GITHUB_TOKEN": "fake-token"}):
        gh = GitHubClient(token="fake-token")

    resp = _make_response(
        403,
        json_body={"message": "Resource not accessible by integration"},
    )
    mock_client = AsyncMock()
    mock_client.request = AsyncMock(return_value=resp)

    with pytest.raises(GitHubAPIError):
        await gh._request_with_client(mock_client, "GET", "/repos/o/r/dependabot/alerts")

    snapshot = get_tracker().snapshot()
    assert len(snapshot) == 1
    assert snapshot[0].scope_hint == "security_events: read"
    assert snapshot[0].method == "GET"
    assert snapshot[0].count == 1


@pytest.mark.asyncio
async def test_403_non_scope_gap_does_not_record() -> None:
    """A generic 403 (admin-rights message) should not feed the tracker."""
    with patch.dict("os.environ", {"GITHUB_TOKEN": "fake-token"}):
        gh = GitHubClient(token="fake-token")

    # "Must have admin rights" DOES match the scope-gap phrasing — use an
    # unrelated 403 instead.
    resp = _make_response(
        403,
        json_body={"message": "Some other forbidden reason"},
    )
    mock_client = AsyncMock()
    mock_client.request = AsyncMock(return_value=resp)

    with pytest.raises(GitHubAPIError):
        await gh._request_with_client(mock_client, "GET", "/repos/o/r/settings")

    assert get_tracker().is_empty()


# ── Body rendering ────────────────────────────────────────────────────


def test_render_issue_body_includes_marker_and_permissions_block() -> None:
    tracker = ScopeGapTracker()
    tracker.record("GET", "/repos/o/r/dependabot/alerts")
    tracker.record("POST", "/repos/o/r/pulls")
    tracker.record("POST", "/repos/o/r/issues/5/assignees")

    body = render_issue_body(tracker.snapshot(), owner="o", repo="r")

    assert SCOPE_GAP_ISSUE_MARKER in body
    # Scopes listed
    assert "security_events: read" in body
    assert "pull_requests: write" in body
    assert "issues: write" in body
    # YAML block present
    assert "permissions:" in body
    assert "contents: read" in body  # baseline always emitted
    # Known endpoint + count appears
    assert "/repos/o/r/dependabot/alerts" in body


def test_render_issue_body_empty_still_deterministic() -> None:
    body = render_issue_body([], owner="o", repo="r")
    assert SCOPE_GAP_ISSUE_MARKER in body
    # baseline permissions always present
    assert "contents: read" in body


# ── Reporter: integration with mock GitHub client ─────────────────────


def _issue(number: int, body: str, *, labels: list[str] | None = None) -> Issue:
    return Issue(
        number=number,
        title=SCOPE_GAP_ISSUE_TITLE,
        body=body,
        state="open",
        user=User(login="me", id=1),
        labels=[Label(name=lbl, color="") for lbl in (labels or [])],
        assignees=[],
        created_at="2026-04-20T00:00:00Z",
        updated_at="2026-04-20T00:00:00Z",
        html_url="http://x",
    )


class FakeGitHub:
    def __init__(self) -> None:
        self.list_calls: list[tuple[str, str, str | None]] = []
        self.create_calls: list[dict] = []
        self.update_calls: list[dict] = []
        self.ensure_label_calls: list[tuple[str, str]] = []
        self.existing_issues: dict[str | None, list[Issue]] = {}

    async def list_issues(
        self, owner: str, repo: str, state: str = "open", labels: str | None = None
    ) -> list[Issue]:
        self.list_calls.append((owner, repo, labels))
        return list(self.existing_issues.get(labels, []))

    async def create_issue(
        self,
        owner: str,
        repo: str,
        title: str,
        body: str,
        labels: list[str] | None = None,
        **_: object,
    ) -> Issue:
        self.create_calls.append(
            {"owner": owner, "repo": repo, "title": title, "body": body, "labels": labels}
        )
        return _issue(42, body, labels=labels)

    async def update_issue(self, owner: str, repo: str, number: int, **kwargs: object) -> Issue:
        self.update_calls.append({"owner": owner, "repo": repo, "number": number, **kwargs})
        return _issue(number, str(kwargs.get("body", "")))

    async def ensure_label(
        self, owner: str, repo: str, name: str, color: str, description: str = ""
    ) -> None:
        self.ensure_label_calls.append((name, color))


@pytest.mark.asyncio
async def test_publish_noop_when_tracker_empty() -> None:
    gh = FakeGitHub()
    result = await publish_scope_gap_issue(gh, "o", "r")
    assert result is None
    assert gh.create_calls == []
    assert gh.update_calls == []


@pytest.mark.asyncio
async def test_publish_creates_when_no_existing_issue() -> None:
    tracker = get_tracker()
    tracker.record("GET", "/repos/o/r/dependabot/alerts")
    tracker.record("POST", "/repos/o/r/pulls")

    gh = FakeGitHub()
    result = await publish_scope_gap_issue(gh, "o", "r")

    assert result == 42
    assert len(gh.create_calls) == 1
    call = gh.create_calls[0]
    assert call["title"] == SCOPE_GAP_ISSUE_TITLE
    assert SCOPE_GAP_LABEL in (call["labels"] or [])
    assert SCOPE_GAP_ACTION_LABEL in (call["labels"] or [])
    assert "permissions:" in call["body"]
    assert SCOPE_GAP_ISSUE_MARKER in call["body"]
    # Labels ensured before create
    ensured = {name for name, _ in gh.ensure_label_calls}
    assert {SCOPE_GAP_LABEL, SCOPE_GAP_ACTION_LABEL} <= ensured


@pytest.mark.asyncio
async def test_publish_updates_existing_labeled_issue() -> None:
    tracker = get_tracker()
    tracker.record("GET", "/repos/o/r/dependabot/alerts")

    gh = FakeGitHub()
    gh.existing_issues[SCOPE_GAP_LABEL] = [
        _issue(17, f"{SCOPE_GAP_ISSUE_MARKER}\n\nold body", labels=[SCOPE_GAP_LABEL])
    ]

    result = await publish_scope_gap_issue(gh, "o", "r")
    assert result == 17
    assert gh.create_calls == []
    assert len(gh.update_calls) == 1
    update = gh.update_calls[0]
    assert update["number"] == 17
    assert update["state"] == "open"
    assert "security_events: read" in update["body"]


@pytest.mark.asyncio
async def test_publish_idempotent_when_body_unchanged() -> None:
    """Second run with identical incidents must not PATCH — avoids churn."""
    tracker = get_tracker()
    tracker.record("GET", "/repos/o/r/dependabot/alerts")

    new_body = render_issue_body(tracker.snapshot(), owner="o", repo="r")

    gh = FakeGitHub()
    gh.existing_issues[SCOPE_GAP_LABEL] = [_issue(17, new_body, labels=[SCOPE_GAP_LABEL])]

    result = await publish_scope_gap_issue(gh, "o", "r")
    assert result == 17
    assert gh.update_calls == []  # no edit — body matches


@pytest.mark.asyncio
async def test_publish_finds_unlabeled_issue_by_marker() -> None:
    tracker = get_tracker()
    tracker.record("POST", "/repos/o/r/pulls")

    gh = FakeGitHub()
    gh.existing_issues[SCOPE_GAP_LABEL] = []  # label search finds nothing
    gh.existing_issues[None] = [_issue(5, f"{SCOPE_GAP_ISSUE_MARKER}\n\nold", labels=[])]

    result = await publish_scope_gap_issue(gh, "o", "r")
    assert result == 5
    assert gh.update_calls and gh.update_calls[0]["number"] == 5


@pytest.mark.asyncio
async def test_publish_dry_run_skips_write() -> None:
    tracker = get_tracker()
    tracker.record("GET", "/repos/o/r/dependabot/alerts")

    gh = FakeGitHub()
    result = await publish_scope_gap_issue(gh, "o", "r", dry_run=True)
    assert result is None
    assert gh.create_calls == []
    assert gh.update_calls == []
