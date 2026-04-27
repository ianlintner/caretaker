"""Tests for orchestrator reconciliation behavior."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest

from caretaker.config import MaintainerConfig
from caretaker.orchestrator import Orchestrator
from caretaker.state.models import (
    IssueTrackingState,
    OrchestratorState,
    PRTrackingState,
    RunSummary,
    TrackedIssue,
    TrackedPR,
)

if TYPE_CHECKING:
    import pathlib


def make_orchestrator() -> Orchestrator:
    github = AsyncMock()
    config = MaintainerConfig()
    return Orchestrator(config=config, github=github, owner="o", repo="r")


class TestOrchestratorReconciliation:
    def test_marks_issue_completed_when_linked_pr_merged(self) -> None:
        orchestrator = make_orchestrator()
        state = OrchestratorState(
            tracked_prs={
                10: TrackedPR(number=10, state=PRTrackingState.MERGED),
            },
            tracked_issues={
                1: TrackedIssue(
                    number=1,
                    state=IssueTrackingState.PR_OPENED,
                    assigned_pr=10,
                ),
            },
        )
        summary = RunSummary(mode="full")

        orchestrator._reconcile_state(state, summary)

        assert state.tracked_issues[1].state == IssueTrackingState.COMPLETED

    def test_counts_orphaned_open_prs(self) -> None:
        orchestrator = make_orchestrator()
        state = OrchestratorState(
            tracked_prs={
                20: TrackedPR(number=20, state=PRTrackingState.CI_PENDING),
                21: TrackedPR(number=21, state=PRTrackingState.MERGED),
            },
            tracked_issues={},
        )
        summary = RunSummary(mode="full")

        orchestrator._reconcile_state(state, summary)

        assert summary.orphaned_prs == 1

    def test_does_not_count_escalated_pr_as_orphan(self) -> None:
        orchestrator = make_orchestrator()
        state = OrchestratorState(
            tracked_prs={
                22: TrackedPR(number=22, state=PRTrackingState.ESCALATED),
            },
            tracked_issues={},
        )
        summary = RunSummary(mode="full")

        orchestrator._reconcile_state(state, summary)

        assert summary.orphaned_prs == 0

    def test_escalates_stale_assignments(self) -> None:
        orchestrator = make_orchestrator()
        old = datetime.now(UTC) - timedelta(days=14)
        state = OrchestratorState(
            tracked_issues={
                3: TrackedIssue(
                    number=3,
                    state=IssueTrackingState.ASSIGNED,
                    last_checked=old,
                ),
            },
        )
        summary = RunSummary(mode="full")

        orchestrator._reconcile_state(state, summary)

        assert state.tracked_issues[3].state == IssueTrackingState.ESCALATED
        assert state.tracked_issues[3].escalated is True
        assert summary.stale_assignments_escalated == 1

    def test_avg_merge_time_tolerates_mixed_naive_and_aware_datetimes(self) -> None:
        """merged_at from GitHub is timezone-aware; first_seen_at is naive UTC.
        _reconcile_state must not raise TypeError when computing avg_time_to_merge_hours."""
        orchestrator = make_orchestrator()
        naive_first_seen = datetime(2024, 1, 1, 0, 0, 0)
        aware_merged_at = datetime(2024, 1, 1, 2, 0, 0, tzinfo=UTC)
        state = OrchestratorState(
            tracked_prs={
                30: TrackedPR(
                    number=30,
                    state=PRTrackingState.MERGED,
                    first_seen_at=naive_first_seen,
                    merged_at=aware_merged_at,
                ),
            },
        )
        summary = RunSummary(mode="full")

        orchestrator._reconcile_state(state, summary)

        assert summary.avg_time_to_merge_hours == pytest.approx(2.0)


class TestOrchestratorReportPath:
    async def test_run_writes_json_report(self, tmp_path: pathlib.Path) -> None:
        """Orchestrator.run writes a JSON report when report_path is provided."""
        orchestrator = make_orchestrator()
        report_file = tmp_path / "report.json"

        # Stub out the registry and state so we can just test report writing
        with (
            patch.object(orchestrator._state_tracker, "load", return_value=OrchestratorState()),
            patch.object(orchestrator._state_tracker, "save"),
            patch.object(orchestrator._state_tracker, "post_run_summary"),
            patch.object(orchestrator._registry, "run_all", new_callable=AsyncMock),
        ):
            await orchestrator.run(mode="dry-run", report_path=str(report_file))

        assert report_file.exists()
        data = json.loads(report_file.read_text())
        assert "mode" in data
        assert "run_at" in data
        assert "errors" in data

    async def test_run_without_report_path_writes_no_file(self, tmp_path: pathlib.Path) -> None:
        """Orchestrator.run does not create any extra files when report_path is None."""
        orchestrator = make_orchestrator()

        with (
            patch.object(orchestrator._state_tracker, "load", return_value=OrchestratorState()),
            patch.object(orchestrator._state_tracker, "save"),
            patch.object(orchestrator._state_tracker, "post_run_summary"),
            patch.object(orchestrator._registry, "run_all", new_callable=AsyncMock),
        ):
            await orchestrator.run(mode="dry-run", report_path=None)

        # No unexpected JSON files in tmp_path
        assert list(tmp_path.glob("*.json")) == []

    async def test_charlie_mode_runs_only_charlie_agent(self) -> None:
        """Charlie mode invokes janitorial cleanup without running the broader cycle."""
        orchestrator = make_orchestrator()

        mock_agent = AsyncMock()
        mock_agent.name = "charlie"
        with (
            patch.object(orchestrator._state_tracker, "load", return_value=OrchestratorState()),
            patch.object(orchestrator._state_tracker, "save"),
            patch.object(orchestrator._state_tracker, "post_run_summary"),
            patch.object(orchestrator._registry, "run_all", new_callable=AsyncMock) as mock_run_all,
            patch.object(orchestrator._registry, "agents_for_mode", return_value=[mock_agent]),
            patch.object(orchestrator._registry, "run_one", new_callable=AsyncMock) as mock_run_one,
        ):
            await orchestrator.run(mode="charlie")

        # Goal engine is disabled by default; legacy run_all path is taken
        mock_run_all.assert_awaited_once()
        assert mock_run_all.call_args.kwargs.get("mode") == "charlie"
        mock_run_one.assert_not_awaited()

    async def test_dry_run_dispatches_full_mode(self) -> None:
        """Dry-run should evaluate the same agent set as full mode."""
        orchestrator = make_orchestrator()

        mock_agent = AsyncMock()
        mock_agent.name = "pr"
        with (
            patch.object(orchestrator._state_tracker, "load", return_value=OrchestratorState()),
            patch.object(orchestrator._state_tracker, "save"),
            patch.object(orchestrator._state_tracker, "post_run_summary"),
            patch.object(orchestrator._registry, "run_all", new_callable=AsyncMock) as mock_run_all,
            patch.object(
                orchestrator._registry,
                "agents_for_mode",
                return_value=[mock_agent],
            ) as mock_afm,
            patch.object(orchestrator._registry, "run_one", new_callable=AsyncMock),
        ):
            await orchestrator.run(mode="dry-run")

        # Goal engine is disabled by default; legacy run_all path is taken with 'full' mode
        mock_run_all.assert_awaited_once()
        assert mock_run_all.call_args.kwargs.get("mode") == "full"
        mock_afm.assert_not_called()

    async def test_self_heal_mode_forwards_event_payload(self) -> None:
        """Self-heal mode should pass event payload through to registry dispatch."""
        orchestrator = make_orchestrator()
        payload = {"workflow_run": {"id": 123}}

        mock_agent = AsyncMock()
        mock_agent.name = "self-heal"
        with (
            patch.object(orchestrator._state_tracker, "load", return_value=OrchestratorState()),
            patch.object(orchestrator._state_tracker, "save"),
            patch.object(orchestrator._state_tracker, "post_run_summary"),
            patch.object(orchestrator._registry, "run_all", new_callable=AsyncMock) as mock_run_all,
            patch.object(orchestrator._registry, "agents_for_mode", return_value=[mock_agent]),
            patch.object(orchestrator._registry, "run_one", new_callable=AsyncMock) as mock_run_one,
        ):
            await orchestrator.run(mode="self-heal", event_payload=payload)

        # Goal engine is disabled by default; legacy run_all path is taken with payload forwarded
        mock_run_all.assert_awaited_once()
        assert mock_run_all.call_args.kwargs.get("mode") == "self-heal"
        assert mock_run_all.call_args.kwargs.get("event_payload") == payload
        mock_run_one.assert_not_awaited()


class TestOrchestratorStateLoadFailure:
    async def test_run_returns_error_when_state_load_fails(self) -> None:
        """If state_tracker.load() raises a non-transient error, the run returns exit code 1."""
        orchestrator = make_orchestrator()

        with (
            patch.object(
                orchestrator._state_tracker,
                "load",
                side_effect=Exception("unexpected internal error during state deserialization"),
            ),
            patch.object(orchestrator._state_tracker, "save"),
            patch.object(orchestrator._state_tracker, "post_run_summary"),
        ):
            result = await orchestrator.run(mode="dry-run")

        assert result == 1

    async def test_run_does_not_raise_when_state_load_fails(self) -> None:
        """If state_tracker.load() raises, the run completes without propagating the exception."""
        orchestrator = make_orchestrator()

        with (
            patch.object(
                orchestrator._state_tracker,
                "load",
                side_effect=RuntimeError("JSON decode error in state file"),
            ),
            patch.object(orchestrator._state_tracker, "save"),
            patch.object(orchestrator._state_tracker, "post_run_summary"),
        ):
            # Should not raise; errors are captured and reported via exit code.
            # A non-transient failure during load → exit 1.
            result = await orchestrator.run(mode="dry-run")

        assert result == 1


class TestTransientErrorGate:
    """T-M1: orchestrator exit-0 when every ``summary.errors`` entry is transient.

    Tested at the error-aggregation boundary so we can verify the gate
    without spinning up the full orchestrator run.
    """

    def test_is_transient_matches_known_substrings(self) -> None:
        from caretaker.orchestrator import is_transient

        assert is_transient(
            "dependabot alerts unavailable: GitHub API error 403: Resource not accessible"
        )
        assert is_transient("code-scanning alerts unavailable")
        assert is_transient("secret-scanning: 403 Forbidden")
        assert is_transient("pr: assignees could not be set for copilot")
        assert is_transient("httpx.ReadTimeout: timed out")
        assert is_transient("GitHub API error 502: Bad gateway")
        assert is_transient("GitHub API error 503: Service unavailable")
        assert is_transient("memory snapshot was empty on this run")
        assert is_transient("connection reset by peer")

    def test_is_transient_rejects_generic_exceptions(self) -> None:
        from caretaker.orchestrator import is_transient

        assert not is_transient("AttributeError: 'NoneType' has no attribute 'items'")
        assert not is_transient("TypeError: unexpected keyword argument 'foo'")
        assert not is_transient("GitHub API error 422: Validation failed")
        # A bare "403" without a recognised sub-system is NOT transient —
        # we only soft-fail on known flappy paths.
        assert not is_transient("GitHub API error 403: something new")

    def test_is_transient_accepts_exception_instance(self) -> None:
        from caretaker.orchestrator import is_transient

        assert is_transient(TimeoutError())
        assert is_transient(TimeoutError("took too long"))
        assert is_transient(ConnectionError("refused"))
        assert not is_transient(ValueError("bad input"))

    def test_bucket_errors_splits_mixed_list(self) -> None:
        from caretaker.orchestrator import _bucket_errors

        errors = [
            "pr: dependabot alerts unavailable: 403",
            "issue: AttributeError: 'NoneType'",
            "security: secret-scanning 403",
        ]
        transient, non_transient = _bucket_errors(errors)
        assert len(transient) == 2
        assert len(non_transient) == 1
        assert "AttributeError" in non_transient[0]

    async def test_run_exits_0_when_all_errors_transient_and_work_landed(self) -> None:
        """All-transient agent errors with real work → exit 0 + soft-fail counter."""
        orchestrator = make_orchestrator()

        async def _fake_run_all(state, summary, *, mode, event_payload):  # type: ignore[no-untyped-def]
            summary.errors.append("pr: dependabot alerts unavailable: 403 Forbidden")
            summary.errors.append("security: secret-scanning 403")
            summary.prs_monitored = 2  # work landed

        with (
            patch.object(orchestrator._state_tracker, "load", return_value=OrchestratorState()),
            patch.object(orchestrator._state_tracker, "save"),
            patch.object(orchestrator._state_tracker, "post_run_summary"),
            patch.object(orchestrator._registry, "run_all", new=_fake_run_all),
            patch("caretaker.orchestrator.record_orchestrator_soft_fail") as mock_soft_fail,
        ):
            result = await orchestrator.run(mode="full")

        assert result == 0
        mock_soft_fail.assert_called_once_with(category="transient")

    async def test_run_exits_1_when_any_error_non_transient(self) -> None:
        """Mixed transient + non-transient → exit 1, no soft-fail counter."""
        orchestrator = make_orchestrator()

        async def _fake_run_all(state, summary, *, mode, event_payload):  # type: ignore[no-untyped-def]
            summary.errors.append("pr: dependabot alerts unavailable: 403 Forbidden")
            summary.errors.append("issue: AttributeError: 'NoneType' has no attribute 'x'")
            summary.prs_monitored = 2

        with (
            patch.object(orchestrator._state_tracker, "load", return_value=OrchestratorState()),
            patch.object(orchestrator._state_tracker, "save"),
            patch.object(orchestrator._state_tracker, "post_run_summary"),
            patch.object(orchestrator._registry, "run_all", new=_fake_run_all),
            patch("caretaker.orchestrator.record_orchestrator_soft_fail") as mock_soft_fail,
        ):
            result = await orchestrator.run(mode="full")

        assert result == 1
        mock_soft_fail.assert_not_called()

    async def test_run_exits_1_when_all_non_transient(self) -> None:
        """All non-transient → exit 1."""
        orchestrator = make_orchestrator()

        async def _fake_run_all(state, summary, *, mode, event_payload):  # type: ignore[no-untyped-def]
            summary.errors.append("issue: AttributeError: boom")
            summary.prs_monitored = 1

        with (
            patch.object(orchestrator._state_tracker, "load", return_value=OrchestratorState()),
            patch.object(orchestrator._state_tracker, "save"),
            patch.object(orchestrator._state_tracker, "post_run_summary"),
            patch.object(orchestrator._registry, "run_all", new=_fake_run_all),
        ):
            result = await orchestrator.run(mode="full")

        assert result == 1

    async def test_run_exits_0_when_all_transient_but_no_work_landed(self) -> None:
        """All-transient errors with no work done → exit 0 + soft-fail counter.

        Rate-limits and other transient conditions can fire before any agents
        run, so work_landed would be False. We must not treat that as a hard
        failure or the self-heal ladder creates duplicate bug issues.
        """
        orchestrator = make_orchestrator()

        async def _fake_run_all(state, summary, *, mode, event_payload):  # type: ignore[no-untyped-def]
            summary.errors.append("pr: dependabot alerts unavailable: 403")
            # no prs_monitored, no issues_triaged, etc.

        with (
            patch.object(orchestrator._state_tracker, "load", return_value=OrchestratorState()),
            patch.object(orchestrator._state_tracker, "save"),
            patch.object(orchestrator._state_tracker, "post_run_summary"),
            patch.object(orchestrator._registry, "run_all", new=_fake_run_all),
            patch("caretaker.orchestrator.record_orchestrator_soft_fail") as mock_soft_fail,
        ):
            result = await orchestrator.run(mode="full")

        assert result == 0
        mock_soft_fail.assert_called_once_with(category="transient")

    async def test_run_exits_0_when_rate_limit_before_agents_run(self) -> None:
        """Rate-limit exception raised during state load → exit 0 (soft-fail).

        This is the exact scenario observed in production: a GitHub 403 rate-limit
        fires during _state_tracker.load() before any agents execute, so
        work_landed is False.  The error is transient; the workflow must exit 0
        so caretaker does not create a self-heal issue and trigger a feedback loop.
        """
        from caretaker.github_client.api import RateLimitError

        orchestrator = make_orchestrator()

        async def _raise_rate_limit() -> OrchestratorState:
            raise RateLimitError(403, "Rate limited. Retry after 26s.", retry_after_seconds=26)

        with (
            patch.object(orchestrator._state_tracker, "load", side_effect=_raise_rate_limit),
            patch.object(orchestrator._state_tracker, "save"),
            patch.object(orchestrator._state_tracker, "post_run_summary"),
            patch("caretaker.orchestrator.record_orchestrator_soft_fail") as mock_soft_fail,
        ):
            result = await orchestrator.run(mode="full")

        assert result == 0
        mock_soft_fail.assert_called_once_with(category="transient")

    async def test_run_exits_0_when_no_errors(self) -> None:
        orchestrator = make_orchestrator()

        async def _fake_run_all(state, summary, *, mode, event_payload):  # type: ignore[no-untyped-def]
            summary.prs_monitored = 5

        with (
            patch.object(orchestrator._state_tracker, "load", return_value=OrchestratorState()),
            patch.object(orchestrator._state_tracker, "save"),
            patch.object(orchestrator._state_tracker, "post_run_summary"),
            patch.object(orchestrator._registry, "run_all", new=_fake_run_all),
        ):
            result = await orchestrator.run(mode="full")

        assert result == 0


class TestExtractPRNumber:
    """Tests for _extract_pr_number helper."""

    def test_pull_request_event_returns_number(self) -> None:
        from caretaker.orchestrator import _extract_pr_number

        payload = {"pull_request": {"number": 42}}
        assert _extract_pr_number("pull_request", payload) == 42

    def test_pull_request_review_event_returns_number(self) -> None:
        from caretaker.orchestrator import _extract_pr_number

        payload = {"pull_request": {"number": 55}, "review": {"state": "approved"}}
        assert _extract_pr_number("pull_request_review", payload) == 55

    def test_check_run_with_pr_returns_number(self) -> None:
        from caretaker.orchestrator import _extract_pr_number

        payload = {"check_run": {"pull_requests": [{"number": 77}]}}
        assert _extract_pr_number("check_run", payload) == 77

    def test_check_run_empty_prs_returns_none(self) -> None:
        from caretaker.orchestrator import _extract_pr_number

        payload = {"check_run": {"pull_requests": []}}
        assert _extract_pr_number("check_run", payload) is None

    def test_check_suite_with_pr_returns_number(self) -> None:
        from caretaker.orchestrator import _extract_pr_number

        payload = {"check_suite": {"pull_requests": [{"number": 33}]}}
        assert _extract_pr_number("check_suite", payload) == 33

    def test_check_suite_empty_prs_returns_none(self) -> None:
        from caretaker.orchestrator import _extract_pr_number

        payload = {"check_suite": {"pull_requests": []}}
        assert _extract_pr_number("check_suite", payload) is None

    def test_missing_key_returns_none(self) -> None:
        from caretaker.orchestrator import _extract_pr_number

        assert _extract_pr_number("pull_request", {}) is None

    def test_wrong_type_returns_none(self) -> None:
        from caretaker.orchestrator import _extract_pr_number

        assert _extract_pr_number("pull_request", {"pull_request": "bad"}) is None

    def test_unknown_event_returns_none(self) -> None:
        from caretaker.orchestrator import _extract_pr_number

        assert _extract_pr_number("workflow_run", {"workflow_run": {}}) is None
