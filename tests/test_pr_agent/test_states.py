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
