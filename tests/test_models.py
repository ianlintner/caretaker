"""Tests for GitHub data models."""

from __future__ import annotations

from caretaker.github_client.models import (
    Issue,
    Label,
    User,
)
from tests.conftest import make_comment, make_pr


class TestPullRequest:
    def test_is_copilot_pr(self) -> None:
        pr = make_pr(user=User(login="copilot-swe-agent[bot]", id=1, type="Bot"))
        assert pr.is_copilot_pr is True

    def test_case_variant_copilot_login_is_recognized(self) -> None:
        pr = make_pr(user=User(login="Copilot", id=1, type="Bot"))
        assert pr.is_copilot_pr is True

    def test_legacy_copilot_login_is_still_recognized(self) -> None:
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
        comment = make_comment(body="<!-- caretaker:task -->Fix this")
        assert comment.is_maintainer_task is True

    def test_is_not_maintainer_task(self) -> None:
        comment = make_comment(body="Just a regular comment")
        assert comment.is_maintainer_task is False

    def test_is_maintainer_result(self) -> None:
        comment = make_comment(body="<!-- caretaker:result -->FIXED")
        assert comment.is_maintainer_result is True


class TestIssue:
    def test_is_maintainer_issue_by_title(self) -> None:

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

    def test_is_maintainer_issue_by_assigned_label(self) -> None:
        issue = Issue(
            number=1,
            title="Regular issue",
            user=User(login="user", id=1, type="User"),
            labels=[Label(name="maintainer:assigned")],
        )
        assert issue.is_maintainer_issue is True

    def test_is_maintainer_issue_by_assignment_marker(self) -> None:
        issue = Issue(
            number=1,
            title="Regular issue",
            body="<!-- caretaker:assignment -->\nTYPE: BUG_SIMPLE",
            user=User(login="user", id=1, type="User"),
        )
        assert issue.is_maintainer_issue is True

    def test_is_maintainer_issue_by_state_marker(self) -> None:
        issue = Issue(
            number=1,
            title="Regular issue",
            body='<!-- maintainer-state:{"tracked_issues":{}}:maintainer-state -->',
            user=User(login="user", id=1, type="User"),
        )
        assert issue.is_maintainer_issue is True

    def test_is_maintainer_issue_by_caretaker_title(self) -> None:
        issue = Issue(
            number=1,
            title="[Caretaker] Human action required — 2026-W16",
            user=User(login="github-actions[bot]", id=1, type="Bot"),
        )
        assert issue.is_maintainer_issue is True

    def test_is_maintainer_issue_by_escalation_digest_label(self) -> None:
        issue = Issue(
            number=1,
            title="Some issue",
            user=User(login="user", id=1, type="User"),
            labels=[Label(name="maintainer:escalation-digest")],
        )
        assert issue.is_maintainer_issue is True

    def test_is_maintainer_issue_by_escalation_digest_marker(self) -> None:
        issue = Issue(
            number=1,
            title="Some issue",
            body="## CI failures\n\n<!-- caretaker:escalation-digest week:2026-W16 -->",
            user=User(login="user", id=1, type="User"),
        )
        assert issue.is_maintainer_issue is True

    def test_not_maintainer_issue(self) -> None:
        issue = Issue(
            number=1,
            title="Bug report",
            user=User(login="user", id=1, type="User"),
        )
        assert issue.is_maintainer_issue is False

    def test_is_copilot_assigned(self) -> None:
        issue = Issue(
            number=2,
            title="Bug report",
            user=User(login="user", id=1, type="User"),
            assignees=[User(login="copilot-swe-agent[bot]", id=7, type="Bot")],
        )
        assert issue.is_copilot_assigned is True

    def test_legacy_copilot_assignment_is_still_recognized(self) -> None:
        issue = Issue(
            number=3,
            title="Bug report",
            user=User(login="user", id=1, type="User"),
            assignees=[User(login="copilot", id=7, type="Bot")],
        )
        assert issue.is_copilot_assigned is True

    def test_case_variant_copilot_assignment_is_recognized(self) -> None:
        issue = Issue(
            number=4,
            title="Bug report",
            user=User(login="user", id=1, type="User"),
            assignees=[User(login="Copilot", id=7, type="Bot")],
        )
        assert issue.is_copilot_assigned is True
