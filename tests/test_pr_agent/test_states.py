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


class TestReadinessRequiredReviews:
    """Readiness scoring respects the required_reviews config knob.

    Regression for rust-oauth2-server PR #172 where the score stayed stuck at
    20% on a solo-dev repo because `required_review_missing` was unconditionally
    added whenever no reviewer had approved.
    """

    def test_required_reviews_zero_grants_review_points_without_approval(self) -> None:
        """When required_reviews=0, a PR with no reviews still scores the 30% review component."""
        pr = make_pr()
        checks = [make_check_run(name="test")]  # passing
        result = evaluate_pr(
            pr,
            checks,
            [],  # no reviews at all
            PRTrackingState.CI_PASSING,
            required_reviews=0,
        )
        assert result.readiness is not None
        assert "required_review_missing" not in result.readiness.blockers
        # 10 (mergeable) + 20 (no automated feedback) + 30 (reviews waived) + 40 (CI) = 100
        assert result.readiness.score == 1.0
        assert result.recommended_state == PRTrackingState.MERGE_READY

    def test_required_reviews_zero_still_blocks_on_changes_requested(self) -> None:
        """Even with required_reviews=0, an explicit changes-requested review blocks."""
        pr = make_pr()
        checks = [make_check_run(name="test")]
        reviews = [make_review(state=ReviewState.CHANGES_REQUESTED, body="nope")]
        result = evaluate_pr(
            pr,
            checks,
            reviews,
            PRTrackingState.CI_PASSING,
            required_reviews=0,
        )
        assert result.readiness is not None
        assert "changes_requested" in result.readiness.blockers
        assert "required_review_missing" not in result.readiness.blockers

    def test_required_reviews_default_one_still_adds_blocker(self) -> None:
        """Default behavior (required_reviews=1) is unchanged — missing review blocks."""
        pr = make_pr()
        checks = [make_check_run(name="test")]
        result = evaluate_pr(pr, checks, [], PRTrackingState.CI_PASSING)  # default=1
        assert result.readiness is not None
        assert "required_review_missing" in result.readiness.blockers
        assert result.readiness.score < 1.0


class TestEvaluatePRAutoMergeGate:
    """Regression for T-S3: MERGE_READY must respect auto_merge policy.

    ``merge.evaluate_merge`` already refuses to merge PR families with
    ``auto_merge.<profile>=false``, but the state machine upstream still
    returned ``MERGE_READY`` — which made the status comment say "ready for
    merge" while caretaker silently refused to act. Thread the policy into
    ``evaluate_pr`` and cap the recommendation at ``CI_PASSING / await_review``.
    """

    def test_human_pr_auto_merge_disabled_does_not_return_merge_ready(self) -> None:
        from caretaker.config import AutoMergeConfig
        from caretaker.github_client.models import User

        human = User(login="human-dev", id=7, type="User")
        pr = make_pr(user=human)
        checks = [make_check_run(name="test")]  # passing
        auto_merge = AutoMergeConfig(human_prs=False)
        result = evaluate_pr(
            pr,
            checks,
            [],  # no reviewers assigned → reviews.pending=True
            PRTrackingState.CI_PASSING,
            auto_merge=auto_merge,
        )
        assert result.recommended_state != PRTrackingState.MERGE_READY
        assert result.recommended_state == PRTrackingState.CI_PASSING
        assert result.recommended_action == "await_review"

    def test_human_pr_auto_merge_enabled_returns_merge_ready(self) -> None:
        from caretaker.config import AutoMergeConfig
        from caretaker.github_client.models import User

        human = User(login="human-dev", id=7, type="User")
        pr = make_pr(user=human)
        checks = [make_check_run(name="test")]
        auto_merge = AutoMergeConfig(human_prs=True)
        result = evaluate_pr(
            pr,
            checks,
            [],
            PRTrackingState.CI_PASSING,
            auto_merge=auto_merge,
        )
        assert result.recommended_state == PRTrackingState.MERGE_READY
        assert result.recommended_action == "merge"

    def test_copilot_pr_auto_merge_disabled_caps_at_ci_passing(self) -> None:
        from caretaker.config import AutoMergeConfig
        from caretaker.github_client.models import User

        copilot = User(login="copilot[bot]", id=8, type="Bot")
        pr = make_pr(user=copilot)
        checks = [make_check_run(name="test")]
        auto_merge = AutoMergeConfig(copilot_prs=False)
        result = evaluate_pr(
            pr,
            checks,
            [],
            PRTrackingState.CI_PASSING,
            auto_merge=auto_merge,
        )
        assert result.recommended_state != PRTrackingState.MERGE_READY

    def test_no_auto_merge_config_preserves_legacy_behavior(self) -> None:
        """When auto_merge is None, the gate is disabled — MERGE_READY returns."""
        pr = make_pr()
        checks = [make_check_run(name="test")]
        result = evaluate_pr(pr, checks, [], PRTrackingState.CI_PASSING)
        assert result.recommended_state == PRTrackingState.MERGE_READY


# ── Fix 3: Copilot PR awaiting workflow approval ─────────────────────


class TestCopilotActionRequired:
    """Fix 3: Copilot PRs with action_required runs must surface action_required_runs
    so the stuck-PR guard in agent.py can suppress escalation."""

    def test_copilot_pr_action_required_surfaced(self) -> None:
        """action_required_runs is populated for Copilot PRs — agent guard can fire."""
        from caretaker.github_client.models import User

        copilot_user = User(login="copilot[bot]", id=1, type="Bot")
        pr = make_pr(user=copilot_user)
        assert pr.is_copilot_pr

        action_req_run = make_check_run(
            name="CI / test",
            status=CheckStatus.COMPLETED,
            conclusion=CheckConclusion.ACTION_REQUIRED,
        )
        result = evaluate_pr(
            pr,
            [action_req_run],
            [],
            PRTrackingState.CI_PENDING,
            auto_approve_workflows=False,
        )
        assert result.recommended_state == PRTrackingState.CI_PENDING
        assert result.recommended_action == "wait"
        assert len(result.ci.action_required_runs) == 1

    def test_copilot_awaiting_approval_guard_condition(self) -> None:
        """Verify the guard condition: is_copilot_pr AND action_required_runs non-empty."""
        from caretaker.github_client.models import User

        copilot_user = User(login="copilot[bot]", id=1, type="Bot")
        copilot_pr = make_pr(user=copilot_user)
        human_pr = make_pr()

        action_req_run = make_check_run(
            name="CI / test",
            conclusion=CheckConclusion.ACTION_REQUIRED,
        )
        ci_eval = evaluate_ci([action_req_run])

        # Copilot PR with action_required → guard suppresses stuck escalation
        assert copilot_pr.is_copilot_pr and bool(ci_eval.action_required_runs)

        # Human PR with action_required → guard does NOT suppress (human-owned stalls are real)
        assert not (human_pr.is_copilot_pr and bool(ci_eval.action_required_runs))


# ── request_review_approve — caretaker PR auto-approval ──────────────


class TestRequestReviewApprove:
    """State machine routes caretaker PRs to auto-approval when eligible."""

    def _passing_run(self) -> object:
        return make_check_run("ci", CheckStatus.COMPLETED, CheckConclusion.SUCCESS)

    def test_caretaker_pr_ci_green_no_review_routes_to_approve(self) -> None:
        """CI green, no reviews → request_review_approve."""
        pr = make_pr()
        pr.head_ref = "claude/fix-something"
        result = evaluate_pr(
            pr,
            [self._passing_run()],
            [],
            PRTrackingState.CI_PASSING,
        )
        assert result.recommended_action == "request_review_approve"

    def test_caretaker_pr_already_approved_does_not_re_approve(self) -> None:
        """If already approved, fall through to MERGE_READY — no re-approval."""
        pr = make_pr()
        pr.head_ref = "claude/fix-something"
        approval = make_review(state=ReviewState.APPROVED)
        result = evaluate_pr(
            pr,
            [self._passing_run()],
            [approval],
            PRTrackingState.CI_PASSING,
        )
        assert result.recommended_action != "request_review_approve"
        assert result.recommended_state == PRTrackingState.MERGE_READY

    def test_caretaker_pr_changes_requested_does_not_approve(self) -> None:
        """CHANGES_REQUESTED blocks auto-approval — must go through fix path."""
        pr = make_pr()
        pr.head_ref = "claude/fix-something"
        blocker = make_review(state=ReviewState.CHANGES_REQUESTED, body="Must fix the auth check")
        result = evaluate_pr(
            pr,
            [self._passing_run()],
            [blocker],
            PRTrackingState.CI_PASSING,
        )
        assert result.recommended_action != "request_review_approve"
        assert result.recommended_action == "request_review_fix"

    def test_caretaker_pr_fix_in_flight_does_not_approve(self) -> None:
        """When a fix is already in-flight (FIX_REQUESTED), skip auto-approval."""
        pr = make_pr()
        pr.head_ref = "caretaker/bump-deps"
        result = evaluate_pr(
            pr,
            [self._passing_run()],
            [],
            PRTrackingState.FIX_REQUESTED,
        )
        assert result.recommended_action != "request_review_approve"

    def test_human_pr_does_not_get_auto_approved(self) -> None:
        """Non-caretaker PRs must not be auto-approved."""
        pr = make_pr()
        pr.head_ref = "feature/my-cool-thing"
        result = evaluate_pr(
            pr,
            [self._passing_run()],
            [],
            PRTrackingState.CI_PASSING,
        )
        assert result.recommended_action != "request_review_approve"

    def test_caretaker_prefix_caretaker_slash_also_routed(self) -> None:
        """caretaker/ branch prefix is also eligible for auto-approval."""
        pr = make_pr()
        pr.head_ref = "caretaker/upgrade-something"
        result = evaluate_pr(
            pr,
            [self._passing_run()],
            [],
            PRTrackingState.CI_PASSING,
        )
        assert result.recommended_action == "request_review_approve"

    def test_maintainer_bot_pr_releases_json_gets_auto_approved(self) -> None:
        """chore/releases-json-* PRs must be eligible for auto-approval."""
        pr = make_pr(head_ref="chore/releases-json-v0.19.5")
        result = evaluate_pr(
            pr,
            [self._passing_run()],
            [],
            PRTrackingState.CI_PASSING,
        )
        assert result.recommended_action == "request_review_approve"

    def test_maintainer_bot_pr_github_actions_chore_gets_auto_approved(self) -> None:
        """github-actions[bot] chore/ PRs must be eligible for auto-approval."""
        from caretaker.github_client.models import User

        pr = make_pr(
            user=User(login="github-actions[bot]", id=1, type="Bot"),
            head_ref="chore/bump-version",
        )
        result = evaluate_pr(
            pr,
            [self._passing_run()],
            [],
            PRTrackingState.CI_PASSING,
        )
        assert result.recommended_action == "request_review_approve"

    def test_maintainer_bot_pr_auto_merge_allowed(self) -> None:
        """_auto_merge_allows returns True for maintainer bot PRs by default."""
        from caretaker.config import AutoMergeConfig
        from caretaker.pr_agent.states import _auto_merge_allows  # type: ignore[attr-defined]

        pr = make_pr(head_ref="chore/releases-json-v0.19.5")
        assert _auto_merge_allows(pr, AutoMergeConfig()) is True

    def test_maintainer_bot_pr_auto_merge_disabled_when_flag_off(self) -> None:
        """_auto_merge_allows returns False when maintainer_bot_prs=False."""
        from caretaker.config import AutoMergeConfig
        from caretaker.pr_agent.states import _auto_merge_allows  # type: ignore[attr-defined]

        pr = make_pr(head_ref="chore/releases-json-v0.19.5")
        assert _auto_merge_allows(pr, AutoMergeConfig(maintainer_bot_prs=False)) is False

    def test_caretaker_pr_auto_merge_allowed_by_default(self) -> None:
        """_auto_merge_allows returns True for caretaker PRs (caretaker_prs defaults True)."""
        from caretaker.config import AutoMergeConfig
        from caretaker.pr_agent.states import _auto_merge_allows  # type: ignore[attr-defined]

        pr = make_pr(head_ref="claude/fix-something")
        assert _auto_merge_allows(pr, AutoMergeConfig()) is True

    def test_caretaker_pr_auto_merge_disabled_when_flag_off(self) -> None:
        """_auto_merge_allows returns False when caretaker_prs=False."""
        from caretaker.config import AutoMergeConfig
        from caretaker.pr_agent.states import _auto_merge_allows  # type: ignore[attr-defined]

        pr = make_pr(head_ref="claude/fix-something")
        assert _auto_merge_allows(pr, AutoMergeConfig(caretaker_prs=False)) is False

    def test_caretaker_pr_does_not_fall_through_to_human_prs(self) -> None:
        """Caretaker PRs must not fall through to the human_prs branch (which defaults False)."""
        from caretaker.config import AutoMergeConfig
        from caretaker.pr_agent.states import _auto_merge_allows  # type: ignore[attr-defined]

        pr = make_pr(head_ref="claude/something")
        # human_prs=False (default), caretaker_prs=True (default) → should allow
        config = AutoMergeConfig(human_prs=False)
        assert _auto_merge_allows(pr, config) is True


# ── evaluate_readiness — automated_feedback blocker exemptions ────────


class TestReadinessAutomatedFeedbackExemptions:
    """Caretaker/maintainer-bot/opted-in PRs must skip automated_feedback_unaddressed."""

    def _bot_review(self) -> object:
        from caretaker.github_client.models import User

        return make_review(
            user=User(login="the-care-taker[bot]", id=99, type="Bot"),
            state=ReviewState.COMMENTED,
            body="Looks good to me.",
        )

    def test_caretaker_pr_skips_automated_feedback_blocker(self) -> None:
        from caretaker.pr_agent.states import evaluate_readiness

        pr = make_pr(head_ref="claude/fix-something")
        ci = evaluate_ci([make_check_run("ci")])
        review_eval = evaluate_reviews([self._bot_review()])
        result = evaluate_readiness(pr, ci, review_eval, PRTrackingState.CI_PASSING)
        assert "automated_feedback_unaddressed" not in result.blockers

    def test_maintainer_bot_pr_skips_automated_feedback_blocker(self) -> None:
        from caretaker.pr_agent.states import evaluate_readiness

        pr = make_pr(head_ref="chore/releases-json-v1.0.0")
        ci = evaluate_ci([make_check_run("ci")])
        review_eval = evaluate_reviews([self._bot_review()])
        result = evaluate_readiness(pr, ci, review_eval, PRTrackingState.CI_PASSING)
        assert "automated_feedback_unaddressed" not in result.blockers

    def test_opted_in_pr_skips_automated_feedback_blocker(self) -> None:
        from caretaker.config import AutoMergeConfig
        from caretaker.github_client.models import Label
        from caretaker.pr_agent.states import evaluate_readiness

        pr = make_pr(labels=[Label(name="caretaker:merge")])
        ci = evaluate_ci([make_check_run("ci")])
        review_eval = evaluate_reviews([self._bot_review()])
        result = evaluate_readiness(
            pr, ci, review_eval, PRTrackingState.CI_PASSING, auto_merge=AutoMergeConfig()
        )
        assert "automated_feedback_unaddressed" not in result.blockers

    def test_plain_human_pr_still_gets_automated_feedback_blocker(self) -> None:
        from caretaker.pr_agent.states import evaluate_readiness

        pr = make_pr(head_ref="feature/my-thing")
        ci = evaluate_ci([make_check_run("ci")])
        review_eval = evaluate_reviews([self._bot_review()])
        result = evaluate_readiness(pr, ci, review_eval, PRTrackingState.CI_PASSING)
        assert "automated_feedback_unaddressed" in result.blockers


# ── Opted-in PR auto-approve and dispatch-loop prevention ────────────


class TestOptedInPRAutoApprove:
    """Human PRs with the merge opt-in label are routed to auto-approve
    and must not trigger a Copilot dispatch loop from COMMENT-type bot reviews."""

    def _passing_run(self) -> object:
        return make_check_run("ci", CheckStatus.COMPLETED, CheckConclusion.SUCCESS)

    def test_opted_in_pr_routes_to_auto_approve(self) -> None:
        """A human PR with caretaker:merge label and CI green → request_review_approve."""
        from caretaker.config import AutoMergeConfig
        from caretaker.github_client.models import Label

        pr = make_pr(labels=[Label(name="caretaker:merge")])
        result = evaluate_pr(
            pr,
            [self._passing_run()],
            [],
            PRTrackingState.CI_PASSING,
            auto_merge=AutoMergeConfig(),
        )
        assert result.recommended_action == "request_review_approve"

    def test_opted_in_pr_bot_comment_does_not_trigger_dispatch(self) -> None:
        """A COMMENTED bot review on an opted-in PR must NOT dispatch request_review_fix."""
        from caretaker.config import AutoMergeConfig
        from caretaker.github_client.models import Label, User

        pr = make_pr(labels=[Label(name="caretaker:merge")])
        bot_review = make_review(
            user=User(login="the-care-taker[bot]", id=99, type="Bot"),
            state=ReviewState.COMMENTED,
            body="Looks reasonable to me; no changes requested.",
        )
        result = evaluate_pr(
            pr,
            [self._passing_run()],
            [bot_review],
            PRTrackingState.CI_PASSING,
            auto_merge=AutoMergeConfig(),
        )
        assert result.recommended_action != "request_review_fix"

    def test_caretaker_pr_bot_comment_does_not_trigger_dispatch(self) -> None:
        """A COMMENTED bot review on a caretaker PR must NOT trigger request_review_fix."""
        from caretaker.github_client.models import User

        pr = make_pr(head_ref="claude/fix-something")
        bot_review = make_review(
            user=User(login="the-care-taker[bot]", id=99, type="Bot"),
            state=ReviewState.COMMENTED,
            body="Code looks fine, merging.",
        )
        result = evaluate_pr(
            pr,
            [self._passing_run()],
            [bot_review],
            PRTrackingState.CI_PASSING,
        )
        assert result.recommended_action != "request_review_fix"
        assert result.recommended_action == "request_review_approve"

    def test_opted_in_pr_changes_requested_still_blocks(self) -> None:
        """CHANGES_REQUESTED on an opted-in PR still blocks merge."""
        from caretaker.config import AutoMergeConfig
        from caretaker.github_client.models import Label

        pr = make_pr(labels=[Label(name="caretaker:merge")])
        blocker = make_review(state=ReviewState.CHANGES_REQUESTED, body="Must fix this")
        result = evaluate_pr(
            pr,
            [self._passing_run()],
            [blocker],
            PRTrackingState.CI_PASSING,
            auto_merge=AutoMergeConfig(),
        )
        assert result.recommended_action == "request_review_fix"

    def test_non_opted_in_human_pr_still_dispatches_for_bot_comments(self) -> None:
        """A plain human PR (no opt-in label) still triggers request_review_fix for bot comments."""
        from caretaker.github_client.models import User

        pr = make_pr(head_ref="feature/my-thing")
        bot_review = make_review(
            user=User(login="copilot-pull-request-reviewer", id=99, type="Bot"),
            state=ReviewState.COMMENTED,
            body="Consider adding error handling here.",
        )
        result = evaluate_pr(
            pr,
            [self._passing_run()],
            [bot_review],
            PRTrackingState.CI_PASSING,
        )
        assert result.recommended_action == "request_review_fix"


# ── Bot CheckRun + comment-marker approvals (PR #609 regression) ─────


class TestBotApprovalChannels:
    """Bot reviewers signal approval through three channels — all should
    satisfy the readiness review-gate. See PR #609 incident: claude-review
    posted a SUCCESS CheckRun, but the comment forever read
    "required_review_missing" because only formal Reviews counted.
    """

    def test_bot_check_run_counts_as_approval(self) -> None:
        """A configured bot CheckRun (e.g. claude-review) success satisfies the gate."""
        from caretaker.github_client.models import CheckConclusion, CheckStatus

        checks = [
            make_check_run(
                name="claude-review",
                status=CheckStatus.COMPLETED,
                conclusion=CheckConclusion.SUCCESS,
            )
        ]
        result = evaluate_reviews([], check_runs=checks, bot_check_names=["claude-review"])
        assert result.approved is True
        assert len(result.bot_check_approvals) == 1
        assert result.has_bot_approval is True

    def test_bot_check_run_pending_does_not_approve(self) -> None:
        """An in-progress bot CheckRun does not pre-approve the PR."""
        from caretaker.github_client.models import CheckStatus

        checks = [
            make_check_run(name="claude-review", status=CheckStatus.IN_PROGRESS, conclusion=None)
        ]
        result = evaluate_reviews([], check_runs=checks, bot_check_names=["claude-review"])
        assert result.approved is False
        assert len(result.bot_check_approvals) == 0

    def test_bot_review_with_marker_counts_as_approval(self) -> None:
        """A bot COMMENTED review whose body matches an approval marker satisfies the gate."""
        from caretaker.github_client.models import User

        bot = User(login="claude[bot]", id=99, type="Bot")
        rev = make_review(user=bot, state=ReviewState.COMMENTED, body="**Approved** — looks good!")
        result = evaluate_reviews([rev], bot_approval_markers=["**approved**", "lgtm"])
        assert result.approved is True
        assert len(result.bot_comment_approvals) == 1

    def test_bot_review_without_marker_stays_automated_feedback(self) -> None:
        """A bot COMMENTED review without a marker remains feedback, not approval."""
        from caretaker.github_client.models import User

        bot = User(login="copilot-pull-request-reviewer[bot]", id=99, type="Bot")
        rev = make_review(user=bot, state=ReviewState.COMMENTED, body="Consider X")
        result = evaluate_reviews([rev], bot_approval_markers=["**approved**"])
        assert result.approved is False
        assert len(result.automated_review_comments) == 1
        assert len(result.bot_comment_approvals) == 0
        assert result.has_automated_comments is True

    def test_bot_issue_comment_with_marker_counts_as_approval(self) -> None:
        """A bot-authored issue comment with 'LGTM' satisfies the gate even without a Review."""
        from caretaker.github_client.models import User
        from tests.conftest import make_comment

        ic = make_comment(body="LGTM!", user=User(login="claude[bot]", id=99, type="Bot"))
        result = evaluate_reviews([], issue_comments=[ic], bot_approval_markers=["lgtm"])
        assert result.approved is True

    def test_changes_requested_overrides_bot_approval(self) -> None:
        """Human CHANGES_REQUESTED still blocks even when a bot CheckRun signed off."""
        from caretaker.github_client.models import CheckConclusion, CheckStatus

        rev_block = make_review(state=ReviewState.CHANGES_REQUESTED, body="Nope")
        checks = [
            make_check_run(
                name="claude-review",
                status=CheckStatus.COMPLETED,
                conclusion=CheckConclusion.SUCCESS,
            )
        ]
        result = evaluate_reviews([rev_block], check_runs=checks, bot_check_names=["claude-review"])
        assert result.approved is False
        assert result.changes_requested is True


class TestCaretakerWorkflowExclusion:
    """Caretaker's own supervisor workflow jobs (dispatch-guard, doctor, etc.)
    must not gate the readiness CI rollup — including them would create the
    same self-deadlock that ``caretaker/pr-readiness`` already avoids.
    """

    def test_pending_caretaker_workflow_job_does_not_block_ci(self) -> None:
        """An in-progress dispatch-guard does not flag ci_pending."""
        from caretaker.github_client.models import CheckStatus

        runs = [
            make_check_run(name="lint"),  # SUCCESS
            make_check_run(name="dispatch-guard", status=CheckStatus.IN_PROGRESS, conclusion=None),
        ]
        ci = evaluate_ci(
            runs,
            caretaker_workflow_jobs=[
                "dispatch-guard",
                "doctor",
                "maintain",
                "self-heal-on-failure",
            ],
        )
        assert ci.status == CIStatus.PASSING
        assert ci.all_completed is True
        assert len(ci.pending_runs) == 0

    def test_failing_caretaker_workflow_job_does_not_fail_ci(self) -> None:
        """A failing maintain job does not flag ci_failing — it's caretaker's own job."""
        runs = [
            make_check_run(name="test"),  # SUCCESS
            make_check_run(name="maintain", conclusion=CheckConclusion.FAILURE),
        ]
        ci = evaluate_ci(runs, caretaker_workflow_jobs=["maintain"])
        assert ci.status == CIStatus.PASSING
        assert len(ci.failed_runs) == 0


class TestPR609Regression:
    """End-to-end regression for PR #609: human PR, claude-review CheckRun
    SUCCESS, all real CI green, caretaker workflow jobs running, no formal
    Reviews API submission. Old behavior gave 70% with required_review_missing.
    New behavior gives 100% ready.
    """

    def test_pr609_scenario_reaches_100_percent(self) -> None:
        from caretaker.github_client.models import CheckConclusion, CheckStatus

        pr = make_pr()
        checks = [
            make_check_run(name="lint"),
            make_check_run(name="test"),
            make_check_run(
                name="claude-review",
                status=CheckStatus.COMPLETED,
                conclusion=CheckConclusion.SUCCESS,
            ),
            # caretaker workflow jobs that should be ignored
            make_check_run(name="dispatch-guard"),
            make_check_run(name="doctor"),
            make_check_run(name="maintain"),
            # caretaker's own readiness check (already in _ALWAYS_IGNORED)
            make_check_run(
                name="caretaker/pr-readiness", status=CheckStatus.IN_PROGRESS, conclusion=None
            ),
        ]
        result = evaluate_pr(
            pr,
            checks,
            [],  # no formal reviews
            PRTrackingState.DISCOVERED,
            required_reviews=1,
            bot_check_names=["claude-review"],
            caretaker_workflow_jobs=[
                "dispatch-guard",
                "doctor",
                "maintain",
                "self-heal-on-failure",
            ],
        )
        assert result.readiness is not None
        assert result.readiness.score == 1.0
        assert result.readiness.conclusion == "success"
        assert result.readiness.blockers == []
        assert result.reviews.has_bot_approval is True
