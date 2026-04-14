"""Tests for issue classification."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from caretaker.config import IssueAgentConfig
from caretaker.github_client.models import Issue, Label, User
from caretaker.issue_agent.classifier import IssueClassification, classify_issue


def make_issue(
    number: int = 1,
    title: str = "Issue title",
    body: str = "Issue body",
    labels: list[Label] | None = None,
    updated_at: datetime | None = None,
) -> Issue:
    return Issue(
        number=number,
        title=title,
        body=body,
        user=User(login="reporter", id=10, type="User"),
        labels=labels or [],
        updated_at=updated_at,
    )


class TestClassifyIssue:
    def test_maintainer_internal(self) -> None:
        issue = make_issue(title="[Maintainer] Internal task")
        result = classify_issue(issue, IssueAgentConfig())
        assert result == IssueClassification.MAINTAINER_INTERNAL

    def test_duplicate_by_label(self) -> None:
        issue = make_issue(labels=[Label(name="duplicate")])
        result = classify_issue(issue, IssueAgentConfig())
        assert result == IssueClassification.DUPLICATE

    def test_duplicate_by_text(self) -> None:
        issue = make_issue(body="This looks like duplicate of #42")
        result = classify_issue(issue, IssueAgentConfig())
        assert result == IssueClassification.DUPLICATE

    def test_stale_by_age(self) -> None:
        old = datetime.now(UTC) - timedelta(days=45)
        issue = make_issue(updated_at=old)
        result = classify_issue(issue, IssueAgentConfig(auto_close_stale_days=30))
        assert result == IssueClassification.STALE

    def test_bug_simple(self) -> None:
        issue = make_issue(title="Bug: parser crash", body="throws exception")
        result = classify_issue(issue, IssueAgentConfig())
        assert result == IssueClassification.BUG_SIMPLE

    def test_feature_large(self) -> None:
        issue = make_issue(
            title="Feature request",
            body="enhancement " + ("details " * 700),
        )
        result = classify_issue(issue, IssueAgentConfig())
        assert result == IssueClassification.FEATURE_LARGE
