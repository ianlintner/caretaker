"""Tests for merge policy evaluation."""

from __future__ import annotations

from caretaker.config import AutoMergeConfig, PRAgentConfig
from caretaker.github_client.models import (
    CheckConclusion,
    Label,
    ReviewState,
    User,
)
from caretaker.pr_agent.merge import evaluate_merge
from caretaker.pr_agent.states import evaluate_ci, evaluate_reviews
from tests.conftest import make_check_run, make_pr, make_review


def _ci_passing():
    return evaluate_ci([make_check_run(name="test")])


def _ci_failing():
    return evaluate_ci([make_check_run(name="test", conclusion=CheckConclusion.FAILURE)])


def _reviews_approved():
    return evaluate_reviews([make_review(state=ReviewState.APPROVED)])


def _reviews_none():
    return evaluate_reviews([])


def _reviews_blocking():
    return evaluate_reviews(
        [
            make_review(state=ReviewState.CHANGES_REQUESTED, body="Fix this"),
        ]
    )


class TestEvaluateMerge:
    def test_copilot_pr_auto_merge(self) -> None:
        pr = make_pr(user=User(login="copilot[bot]", id=1, type="Bot"))
        config = PRAgentConfig()
        decision = evaluate_merge(pr, _ci_passing(), _reviews_approved(), config)
        assert decision.should_merge is True
        assert decision.method == "squash"

    def test_copilot_pr_auto_merge_disabled(self) -> None:
        pr = make_pr(user=User(login="copilot[bot]", id=1, type="Bot"))
        config = PRAgentConfig(
            auto_merge=AutoMergeConfig(copilot_prs=False),
        )
        decision = evaluate_merge(pr, _ci_passing(), _reviews_approved(), config)
        assert decision.should_merge is False
        assert "disabled for Copilot" in decision.reason

    def test_dependabot_pr_auto_merge(self) -> None:
        pr = make_pr(user=User(login="dependabot[bot]", id=2, type="Bot"))
        config = PRAgentConfig()
        decision = evaluate_merge(pr, _ci_passing(), _reviews_approved(), config)
        assert decision.should_merge is True

    def test_dependabot_pr_auto_merge_disabled(self) -> None:
        pr = make_pr(user=User(login="dependabot[bot]", id=2, type="Bot"))
        config = PRAgentConfig(
            auto_merge=AutoMergeConfig(dependabot_prs=False),
        )
        decision = evaluate_merge(pr, _ci_passing(), _reviews_approved(), config)
        assert decision.should_merge is False

    def test_human_pr_no_auto_merge_by_default(self) -> None:
        pr = make_pr()
        config = PRAgentConfig()
        decision = evaluate_merge(pr, _ci_passing(), _reviews_approved(), config)
        assert decision.should_merge is False
        assert "disabled for human" in decision.reason

    def test_human_pr_auto_merge_enabled(self) -> None:
        pr = make_pr()
        config = PRAgentConfig(
            auto_merge=AutoMergeConfig(human_prs=True),
        )
        decision = evaluate_merge(pr, _ci_passing(), _reviews_approved(), config)
        assert decision.should_merge is True

    def test_ci_failing_blocks_merge(self) -> None:
        pr = make_pr(user=User(login="copilot[bot]", id=1, type="Bot"))
        config = PRAgentConfig()
        decision = evaluate_merge(pr, _ci_failing(), _reviews_approved(), config)
        assert decision.should_merge is False
        assert any("CI status" in b for b in decision.blockers)

    def test_changes_requested_blocks_merge(self) -> None:
        pr = make_pr(user=User(login="copilot[bot]", id=1, type="Bot"))
        config = PRAgentConfig()
        decision = evaluate_merge(pr, _ci_passing(), _reviews_blocking(), config)
        assert decision.should_merge is False
        assert any("Changes requested" in b for b in decision.blockers)

    def test_draft_blocks_merge(self) -> None:
        pr = make_pr(user=User(login="copilot[bot]", id=1, type="Bot"), draft=True)
        config = PRAgentConfig()
        decision = evaluate_merge(pr, _ci_passing(), _reviews_approved(), config)
        assert decision.should_merge is False
        assert any("draft" in b for b in decision.blockers)

    def test_merge_conflicts_block(self) -> None:
        pr = make_pr(user=User(login="copilot[bot]", id=1, type="Bot"), mergeable=False)
        config = PRAgentConfig()
        decision = evaluate_merge(pr, _ci_passing(), _reviews_approved(), config)
        assert decision.should_merge is False
        assert any("conflict" in b for b in decision.blockers)

    def test_breaking_label_blocks(self) -> None:
        pr = make_pr(
            user=User(login="copilot[bot]", id=1, type="Bot"),
            labels=[Label(name="maintainer:breaking")],
        )
        config = PRAgentConfig()
        decision = evaluate_merge(pr, _ci_passing(), _reviews_approved(), config)
        assert decision.should_merge is False
        assert any("breaking" in b for b in decision.blockers)

    def test_multiple_blockers(self) -> None:
        pr = make_pr(
            user=User(login="copilot[bot]", id=1, type="Bot"),
            draft=True,
            mergeable=False,
        )
        config = PRAgentConfig()
        decision = evaluate_merge(pr, _ci_failing(), _reviews_blocking(), config)
        assert decision.should_merge is False
        assert len(decision.blockers) >= 3

    def test_maintainer_bot_pr_can_merge(self) -> None:
        """chore/releases-json PRs may merge when CI passes and maintainer_bot_prs=True."""
        pr = make_pr(head_ref="chore/releases-json-v0.19.5")
        config = PRAgentConfig()
        decision = evaluate_merge(pr, _ci_passing(), _reviews_approved(), config)
        assert decision.should_merge is True

    def test_maintainer_bot_pr_blocked_when_flag_off(self) -> None:
        """chore/releases-json PRs are blocked when maintainer_bot_prs=False."""
        from caretaker.config import AutoMergeConfig

        pr = make_pr(head_ref="chore/releases-json-v0.19.5")
        config = PRAgentConfig(auto_merge=AutoMergeConfig(maintainer_bot_prs=False))
        decision = evaluate_merge(pr, _ci_passing(), _reviews_approved(), config)
        assert decision.should_merge is False
        assert any("maintainer-bot" in b for b in decision.blockers)

    def test_maintainer_bot_pr_github_actions_chore_can_merge(self) -> None:
        """github-actions[bot] chore/ PRs can merge when flag is True."""
        pr = make_pr(
            user=User(login="github-actions[bot]", id=1, type="Bot"),
            head_ref="chore/update-releases",
        )
        config = PRAgentConfig()
        decision = evaluate_merge(pr, _ci_passing(), _reviews_approved(), config)
        assert decision.should_merge is True
