"""Tests for PR state machine evaluation."""

from __future__ import annotations

from datetime import UTC

from caretaker.github_client.models import (
    CheckConclusion,
    CheckStatus,
    ReviewState,
)
from caretaker.pr_agent.states import (
    CIStatus,
    evaluate_ci,
    evaluate_pr,
    evaluate_reviews,
)
from caretaker.state.models import PRTrackingState
from tests.conftest import make_check_run, make_pr, make_review

# ── evaluate_ci ──────────────────────────────────────────────────────


class TestEvaluateCI:
    def test_empty_check_runs(self) -> None:
        result = evaluate_ci([])
        assert result.status == CIStatus.PENDING
        assert result.all_completed is True

    def test_all_passing(self) -> None:
        runs = [
            make_check_run(name="lint"),
            make_check_run(name="test"),
        ]
        result = evaluate_ci(runs)
        assert result.status == CIStatus.PASSING
        assert len(result.passed_runs) == 2
        assert result.all_completed is True

    def test_all_failing(self) -> None:
        runs = [
            make_check_run(name="test", conclusion=CheckConclusion.FAILURE),
            make_check_run(name="lint", conclusion=CheckConclusion.FAILURE),
        ]
        result = evaluate_ci(runs)
        assert result.status == CIStatus.FAILING
        assert len(result.failed_runs) == 2

    def test_mixed_results(self) -> None:
        runs = [
            make_check_run(name="lint"),  # passing
            make_check_run(name="test", conclusion=CheckConclusion.FAILURE),
        ]
        result = evaluate_ci(runs)
        assert result.status == CIStatus.MIXED
        assert len(result.failed_runs) == 1
        assert len(result.passed_runs) == 1

    def test_pending_runs(self) -> None:
        runs = [
            make_check_run(name="lint"),
            make_check_run(
                name="test",
                status=CheckStatus.IN_PROGRESS,
                conclusion=None,
            ),
        ]
        result = evaluate_ci(runs)
        assert result.status == CIStatus.PENDING
        assert result.all_completed is False
        assert len(result.pending_runs) == 1

    def test_ignore_jobs(self) -> None:
        runs = [
            make_check_run(name="lint"),
            make_check_run(name="deploy", conclusion=CheckConclusion.FAILURE),
        ]
        result = evaluate_ci(runs, ignore_jobs=["deploy"])
        assert result.status == CIStatus.PASSING
        assert len(result.passed_runs) == 1

    def test_timed_out_counts_as_failure(self) -> None:
        runs = [
            make_check_run(name="test", conclusion=CheckConclusion.TIMED_OUT),
        ]
        result = evaluate_ci(runs)
        assert result.status == CIStatus.FAILING
        assert len(result.failed_runs) == 1

    def test_action_required_is_pending(self) -> None:
        runs = [
            make_check_run(
                name="test",
                status=CheckStatus.COMPLETED,
                conclusion=CheckConclusion.ACTION_REQUIRED,
            ),
        ]
        result = evaluate_ci(runs)
        assert result.status == CIStatus.PENDING
        assert len(result.action_required_runs) == 1
        assert result.all_completed is True

    def test_queued_is_pending(self) -> None:
        runs = [
            make_check_run(
                name="test",
                status=CheckStatus.QUEUED,
                conclusion=None,
            ),
        ]
        result = evaluate_ci(runs)
        assert result.status == CIStatus.PENDING
        assert result.all_completed is False

    def test_waiting_is_pending(self) -> None:
        """GitHub 'waiting' status (e.g. environment gates) must be treated as pending."""
        runs = [
            make_check_run(
                name="deploy",
                status=CheckStatus.WAITING,
                conclusion=None,
            ),
        ]
        result = evaluate_ci(runs)
        assert result.status == CIStatus.PENDING
        assert result.all_completed is False
        assert len(result.pending_runs) == 1

    def test_requested_is_pending(self) -> None:
        """GitHub 'requested' status must be treated as pending CI."""
        runs = [
            make_check_run(
                name="test",
                status=CheckStatus.REQUESTED,
                conclusion=None,
            ),
        ]
        result = evaluate_ci(runs)
        assert result.status == CIStatus.PENDING
        assert result.all_completed is False

    def test_pending_status_is_pending(self) -> None:
        """GitHub 'pending' status must be treated as pending CI."""
        runs = [
            make_check_run(
                name="test",
                status=CheckStatus.PENDING,
                conclusion=None,
            ),
        ]
        result = evaluate_ci(runs)
        assert result.status == CIStatus.PENDING
        assert result.all_completed is False


# ── evaluate_reviews ─────────────────────────────────────────────────


class TestEvaluateReviews:
    def test_no_reviews(self) -> None:
        result = evaluate_reviews([])
        assert result.pending is True
        assert result.approved is False
        assert result.changes_requested is False

    def test_single_approval(self) -> None:
        reviews = [make_review(state=ReviewState.APPROVED)]
        result = evaluate_reviews(reviews)
        assert result.approved is True
        assert result.changes_requested is False

    def test_changes_requested(self) -> None:
        reviews = [make_review(state=ReviewState.CHANGES_REQUESTED, body="Fix this")]
        result = evaluate_reviews(reviews)
        assert result.approved is False
        assert result.changes_requested is True
        assert len(result.blocking_reviews) == 1

    def test_approval_and_changes_requested(self) -> None:
        """Changes requested by one reviewer blocks even with another approval."""
        from caretaker.github_client.models import User

        r1 = make_review(
            user=User(login="approver", id=10, type="User"),
            state=ReviewState.APPROVED,
        )
        r2 = make_review(
            user=User(login="blocker", id=11, type="User"),
            state=ReviewState.CHANGES_REQUESTED,
            body="Needs work",
        )
        result = evaluate_reviews([r1, r2])
        assert result.approved is False
        assert result.changes_requested is True

    def test_latest_review_per_user_wins(self) -> None:
        """If a user first requests changes then approves, the approval wins."""
        from datetime import datetime

        from caretaker.github_client.models import User

        user = User(login="reviewer", id=10, type="User")
        r1 = make_review(
            user=user,
            state=ReviewState.CHANGES_REQUESTED,
            body="Needs work",
            submitted_at=datetime(2024, 1, 1, tzinfo=UTC),
        )
        r2 = make_review(
            user=user,
            state=ReviewState.APPROVED,
            submitted_at=datetime(2024, 1, 2, tzinfo=UTC),
        )
        result = evaluate_reviews([r1, r2])
        assert result.approved is True
        assert result.changes_requested is False


# ── evaluate_pr ──────────────────────────────────────────────────────


class TestEvaluatePR:
    def test_merged_pr(self) -> None:
        pr = make_pr(merged=True, state="closed")
        result = evaluate_pr(pr, [], [], PRTrackingState.DISCOVERED)
        assert result.recommended_state == PRTrackingState.MERGED
        assert result.recommended_action == "none"

    def test_closed_pr(self) -> None:
        pr = make_pr(state="closed")
        result = evaluate_pr(pr, [], [], PRTrackingState.DISCOVERED)
        assert result.recommended_state == PRTrackingState.CLOSED

    def test_ci_pending(self) -> None:
        pr = make_pr()
        checks = [
            make_check_run(name="test", status=CheckStatus.IN_PROGRESS, conclusion=None),
        ]
        result = evaluate_pr(pr, checks, [], PRTrackingState.DISCOVERED)
        assert result.recommended_state == PRTrackingState.CI_PENDING
        assert result.recommended_action == "wait"

    def test_ci_action_required_approve_workflows(self) -> None:
        from caretaker.github_client.models import User

        pr = make_pr(user=User(login="copilot[bot]", id=1, type="Bot"))
        checks = [
            make_check_run(
                name="test",
                status=CheckStatus.COMPLETED,
                conclusion=CheckConclusion.ACTION_REQUIRED,
            ),
        ]
        result = evaluate_pr(
            pr, checks, [], PRTrackingState.DISCOVERED, auto_approve_workflows=True
        )
        assert result.recommended_state == PRTrackingState.CI_PENDING
        assert result.recommended_action == "approve_workflows"

    def test_ci_action_required_untrusted_pr_waits(self) -> None:
        """Untrusted human PRs with action_required runs should wait, not auto-approve."""
        pr = make_pr()  # default is human user
        checks = [
            make_check_run(
                name="test",
                status=CheckStatus.COMPLETED,
                conclusion=CheckConclusion.ACTION_REQUIRED,
            ),
        ]
        result = evaluate_pr(
            pr, checks, [], PRTrackingState.DISCOVERED, auto_approve_workflows=True
        )
        assert result.recommended_state == PRTrackingState.CI_PENDING
        assert result.recommended_action == "wait"

    def test_ci_action_required_flag_off_waits(self) -> None:
        """When auto_approve_workflows is False, always wait even for trusted PRs."""
        from caretaker.github_client.models import User

        pr = make_pr(user=User(login="copilot[bot]", id=1, type="Bot"))
        checks = [
            make_check_run(
                name="test",
                status=CheckStatus.COMPLETED,
                conclusion=CheckConclusion.ACTION_REQUIRED,
            ),
        ]
        result = evaluate_pr(pr, checks, [], PRTrackingState.DISCOVERED)
        assert result.recommended_state == PRTrackingState.CI_PENDING
        assert result.recommended_action == "wait"

    def test_ci_failing_request_fix(self) -> None:
        pr = make_pr()
        checks = [
            make_check_run(name="test", conclusion=CheckConclusion.FAILURE),
        ]
        result = evaluate_pr(pr, checks, [], PRTrackingState.DISCOVERED)
        assert result.recommended_state == PRTrackingState.CI_FAILING
        assert result.recommended_action == "request_fix"

    def test_ci_failing_wait_for_fix(self) -> None:
        """If we already requested a fix, wait for it."""
        pr = make_pr()
        checks = [
            make_check_run(name="test", conclusion=CheckConclusion.FAILURE),
        ]
        result = evaluate_pr(pr, checks, [], PRTrackingState.FIX_REQUESTED)
        assert result.recommended_state == PRTrackingState.CI_FAILING
        assert result.recommended_action == "wait_for_fix"

    def test_ci_passing_no_reviews_merge_ready(self) -> None:
        pr = make_pr()
        checks = [make_check_run(name="test")]
        result = evaluate_pr(pr, checks, [], PRTrackingState.CI_PASSING)
        assert result.recommended_state == PRTrackingState.MERGE_READY
        assert result.recommended_action == "merge"

    def test_ci_passing_approved_merge_ready(self) -> None:
        pr = make_pr()
        checks = [make_check_run(name="test")]
        reviews = [make_review(state=ReviewState.APPROVED)]
        result = evaluate_pr(pr, checks, reviews, PRTrackingState.CI_PASSING)
        assert result.recommended_state == PRTrackingState.MERGE_READY

    def test_ci_passing_changes_requested(self) -> None:
        pr = make_pr()
        checks = [make_check_run(name="test")]
        reviews = [make_review(state=ReviewState.CHANGES_REQUESTED, body="Fix")]
        result = evaluate_pr(pr, checks, reviews, PRTrackingState.CI_PASSING)
        assert result.recommended_state == PRTrackingState.REVIEW_CHANGES_REQUESTED
        assert result.recommended_action == "request_review_fix"

    def test_ci_passing_automated_bot_comment_triggers_review_fix(self) -> None:
        """A COMMENTED review from an automated reviewer bot must trigger request_review_fix."""
        from caretaker.github_client.models import User

        pr = make_pr()
        checks = [make_check_run(name="test")]
        bot_review = make_review(
            user=User(login="copilot-pull-request-reviewer", id=99, type="Bot"),
            state=ReviewState.COMMENTED,
            body="Consider reconciling state before skipping. Also check API usage.",
        )
        result = evaluate_pr(pr, checks, [bot_review], PRTrackingState.CI_PASSING)
        assert result.recommended_state == PRTrackingState.REVIEW_CHANGES_REQUESTED
        assert result.recommended_action == "request_review_fix"
        assert result.reviews.has_automated_comments

    def test_ci_passing_bot_comment_empty_body_does_not_trigger(self) -> None:
        """A COMMENTED review with no body from a bot must NOT trigger request_review_fix."""
        from caretaker.github_client.models import User

        pr = make_pr()
        checks = [make_check_run(name="test")]
        bot_review = make_review(
            user=User(login="copilot-pull-request-reviewer", id=99, type="Bot"),
            state=ReviewState.COMMENTED,
            body="",  # empty — no actionable content
        )
        result = evaluate_pr(pr, checks, [bot_review], PRTrackingState.CI_PASSING)
        # Should be MERGE_READY (no reviews with content, CI passing)
        assert result.recommended_action != "request_review_fix"
        assert not result.reviews.has_automated_comments


# ── evaluate_reviews — automated comment detection ────────────────


class TestEvaluateReviewsAutomated:
    def test_bot_commented_review_is_detected(self) -> None:
        from caretaker.github_client.models import User

        reviews = [
            make_review(
                user=User(login="copilot-pull-request-reviewer", id=99, type="Bot"),
                state=ReviewState.COMMENTED,
                body="Suggestion: fix the state handling",
            )
        ]
        result = evaluate_reviews(reviews)
        assert result.has_automated_comments
        assert len(result.automated_review_comments) == 1

    def test_human_commented_review_is_not_automated(self) -> None:
        reviews = [make_review(state=ReviewState.COMMENTED, body="Looks fine")]
        result = evaluate_reviews(reviews)
        assert not result.has_automated_comments

    def test_generic_bot_login_suffix_is_detected(self) -> None:
        from caretaker.github_client.models import User

        reviews = [
            make_review(
                user=User(login="some-custom-review[bot]", id=100, type="Bot"),
                state=ReviewState.COMMENTED,
                body="Please address this issue",
            )
        ]
        result = evaluate_reviews(reviews)
        assert result.has_automated_comments


class TestEvaluatePRReviewGuards:
    """Tests for wait_for_fix guards on review-related recommendations."""

    def test_changes_requested_waits_when_fix_already_requested(self) -> None:
        """If a fix was already requested, CHANGES_REQUESTED should wait, not re-request."""
        pr = make_pr()
        checks = [make_check_run(name="test")]
        reviews = [make_review(state=ReviewState.CHANGES_REQUESTED, body="Fix this")]
        result = evaluate_pr(pr, checks, reviews, PRTrackingState.FIX_REQUESTED)
        assert result.recommended_state == PRTrackingState.REVIEW_CHANGES_REQUESTED
        assert result.recommended_action == "wait_for_fix"

    def test_automated_comments_dont_retrigger_after_fix_requested(self) -> None:
        """Once a fix has been requested for bot comments, don't re-request — proceed."""
        from caretaker.github_client.models import User

        pr = make_pr()
        checks = [make_check_run(name="test")]
        bot_review = make_review(
            user=User(login="copilot-pull-request-reviewer", id=99, type="Bot"),
            state=ReviewState.COMMENTED,
            body="Consider reconciling state before skipping.",
        )
        result = evaluate_pr(pr, checks, [bot_review], PRTrackingState.FIX_REQUESTED)
        # Should fall through to merge — bot comments already addressed
        assert result.recommended_action in ("merge", "await_review")
        assert result.recommended_action != "request_review_fix"
