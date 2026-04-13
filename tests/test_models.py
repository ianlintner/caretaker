"""Tests for GitHub data models."""

from __future__ import annotations

from project_maintainer.github_client.models import (
    Comment,
    Issue,
    Label,
    PullRequest,
    User,
)

from tests.conftest import make_comment, make_pr


class TestPullRequest:
    def test_is_copilot_pr(self) -> None:
        pr = make_pr(user=User(login="copilot[bot]", id=1, type="Bot"))
        assert pr.is_copilot_pr is True

    def test_is_not_copilot_pr(self) -> None:
        pr = make_pr(user=User(login="dev-user", id=3, type="User"))
        assert pr.is_copilot_pr is False

    def test_is_dependabot_pr(self) -> None:
        pr = make_pr(user=User(login="dependabot[bot]", id=2, type="Bot"))
        assert pr.is_dependabot_pr is True

    def test_is_maintainer_pr(self) -> None:
        pr = make_pr(labels=[Label(name="maintainer:internal")])
        assert pr.is_maintainer_pr is True

    def test_is_not_maintainer_pr(self) -> None:
        pr = make_pr(labels=[Label(name="bug")])
        assert pr.is_maintainer_pr is False

    def test_has_label(self) -> None:
        pr = make_pr(labels=[Label(name="bug"), Label(name="urgent")])
        assert pr.has_label("bug") is True
        assert pr.has_label("feature") is False

    def test_no_labels(self) -> None:
        pr = make_pr()
        assert pr.has_label("anything") is False


class TestComment:
    def test_is_maintainer_task(self) -> None:
        comment = make_comment(body="<!-- project-maintainer:task -->Fix this")
        assert comment.is_maintainer_task is True

    def test_is_not_maintainer_task(self) -> None:
        comment = make_comment(body="Just a regular comment")
        assert comment.is_maintainer_task is False

    def test_is_maintainer_result(self) -> None:
        comment = make_comment(body="<!-- project-maintainer:result -->FIXED")
        assert comment.is_maintainer_result is True


class TestIssue:
    def test_is_maintainer_issue_by_title(self) -> None:
        from datetime import datetime, timezone

        issue = Issue(
            number=1,
            title="[Maintainer] Fix lint errors",
            user=User(login="bot", id=1, type="Bot"),
        )
        assert issue.is_maintainer_issue is True

    def test_is_maintainer_issue_by_label(self) -> None:
        issue = Issue(
            number=1,
            title="Regular issue",
            user=User(login="user", id=1, type="User"),
            labels=[Label(name="maintainer:internal")],
        )
        assert issue.is_maintainer_issue is True

    def test_not_maintainer_issue(self) -> None:
        issue = Issue(
            number=1,
            title="Bug report",
            user=User(login="user", id=1, type="User"),
        )
        assert issue.is_maintainer_issue is False
