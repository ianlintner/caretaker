"""Tests for self-heal failure classification."""

from __future__ import annotations

import gzip
import io
import zipfile
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from caretaker.self_heal_agent.agent import (
    FailureKind,
    SelfHealAgent,
    _classify_failure,
    _clean_log_snippet,
    _decode_job_log_payload,
    _extract_error_context,
    _extract_failing_step,
    _extract_first_error,
)


class TestExtractFirstError:
    def test_prefers_github_actions_error_annotation(self) -> None:
        log = (
            "2026-04-14T23:22:44Z INFO GitHub API error 403: resource not accessible\n"
            "2026-04-14T23:22:52Z ##[error]Process completed with exit code 1.\n"
            "2026-04-14T23:22:53Z   Python_ROOT_DIR: /opt/hostedtoolcache/Python/3.12.13/x64\n"
        )
        result = _extract_first_error(log)
        assert result == "Process completed with exit code 1."

    def test_detects_exit_code_line_without_annotation(self) -> None:
        log = (
            "2026-04-14T23:22:53Z   Python_ROOT_DIR: /opt/hostedtoolcache/Python/3.12.13/x64\n"
            "2026-04-14T23:22:54Z Process completed with exit code 1.\n"
            "2026-04-14T23:22:55Z Post job cleanup.\n"
        )
        result = _extract_first_error(log)
        assert result == "Process completed with exit code 1."

    def test_prefers_non_zero_exit_code_when_success_line_appears_first(self) -> None:
        log = (
            "2026-04-14T23:22:53Z Process completed with exit code 0.\n"
            "2026-04-14T23:22:54Z Uploading artifacts\n"
            "2026-04-14T23:22:55Z Process completed with exit code 1.\n"
            "2026-04-14T23:22:56Z Post job cleanup.\n"
        )
        result = _extract_first_error(log)
        assert result == "Process completed with exit code 1."

    def test_falls_back_to_keyword_scan_when_no_annotation(self) -> None:
        log = (
            "2026-04-14T23:22:44Z Some normal setup line\n"
            "2026-04-14T23:22:52Z FAILED to run linter checks\n"
            "2026-04-14T23:22:53Z Cleaning up files\n"
        )
        result = _extract_first_error(log)
        assert "FAILED" in result

    def test_returns_truncated_text_when_no_keywords(self) -> None:
        log = "short line\nanother short line\n"
        result = _extract_first_error(log)
        assert result == log.strip()[:200]


class TestClassifyFailureUnknown:
    def test_unknown_classification_uses_full_log_for_error_message(self) -> None:
        """Realistic scenario: error annotation buried among noise in a long log."""
        early_noise = "\n".join(
            f"2026-04-14T23:22:{i:02d}Z Collecting package-{i}" for i in range(50)
        )
        caretaker_output = (
            "2026-04-14T23:22:44Z INFO dependabot alerts unavailable: "
            "GitHub API error 403: resource not accessible\n"
            "2026-04-14T23:22:52Z WARNING Run completed with 1 errors\n"
        )
        error_line = "2026-04-14T23:22:52Z ##[error]Process completed with exit code 1."
        noisy_tail = "\n".join(
            [
                "2026-04-14T23:22:53Z   Python_ROOT_DIR: /opt/hostedtoolcache/Python/3.12.13/x64",
                "2026-04-14T23:22:53Z   Python2_ROOT_DIR: /opt/hostedtoolcache/Python/3.12.13/x64",
                "2026-04-14T23:22:53Z   Python3_ROOT_DIR: /opt/hostedtoolcache/Python/3.12.13/x64",
            ]
        )
        log_text = f"{early_noise}\n{caretaker_output}{error_line}\n{noisy_tail}"

        kind, title, details = _classify_failure("maintain", log_text)

        assert kind == FailureKind.UNKNOWN
        assert "Process completed with exit code 1" in title
        assert "Process completed with exit code 1" in details
        assert "Python_ROOT_DIR" not in title
        assert "dependabot" not in title


class TestDecodeJobLogPayload:
    def test_decodes_zip_payload(self) -> None:
        log = (
            "2026-04-14T23:22:52Z ##[error]Process completed with exit code 1.\n"
            "2026-04-14T23:22:53Z   Python_ROOT_DIR: /opt/hostedtoolcache/Python/3.12.13/x64\n"
        )
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, mode="w") as archive:
            archive.writestr("maintain/5_Run.txt", log)

        decoded = _decode_job_log_payload(payload.getvalue(), fallback_text="garbled")

        kind, title, details = _classify_failure("maintain", decoded)
        assert kind == FailureKind.UNKNOWN
        assert "Process completed with exit code 1" in title
        assert "Process completed with exit code 1" in details
        assert "Python_ROOT_DIR" not in title

    def test_decodes_gzip_payload(self) -> None:
        log = "2026-04-14T23:22:52Z ##[error]Process completed with exit code 1.\n"
        payload = gzip.compress(log.encode("utf-8"))

        decoded = _decode_job_log_payload(payload, fallback_text="garbled")

        assert "Process completed with exit code 1" in decoded


@pytest.mark.asyncio
class TestSelfHealActionedSigs:
    async def test_transient_failures_are_not_recorded_as_actioned(self) -> None:
        github = AsyncMock()
        agent = SelfHealAgent(github=github, owner="o", repo="r", report_upstream=False)

        with (
            patch.object(
                agent,
                "_collect_failure_logs",
                AsyncMock(return_value=[("maintain", "transient log")]),
            ),
            patch.object(agent, "_get_existing_self_heal_sigs", AsyncMock(return_value=set())),
            patch.object(agent, "_run_id_already_tracked", AsyncMock(return_value=False)),
            patch(
                "caretaker.self_heal_agent.agent._classify_failure",
                return_value=(FailureKind.TRANSIENT, "Transient timeout", "retry later"),
            ),
        ):
            report = await agent.run()

        assert report.actioned_sigs == []


@pytest.mark.asyncio
class TestSelfHealCrossAgentDedup:
    async def test_skips_when_run_id_already_tracked_by_devops(self) -> None:
        github = AsyncMock()
        agent = SelfHealAgent(github=github, owner="o", repo="r", report_upstream=False)

        with (
            patch.object(
                agent,
                "_collect_failure_logs",
                AsyncMock(return_value=[("maintain", "some error log")]),
            ),
            patch.object(agent, "_get_existing_self_heal_sigs", AsyncMock(return_value=set())),
            patch.object(agent, "_run_id_already_tracked", AsyncMock(return_value=True)),
        ):
            payload = {"workflow_run": {"id": 99999, "conclusion": "failure"}}
            report = await agent.run(event_payload=payload)

        assert report.failures_analyzed == 1
        assert report.local_issues_created == []
        assert report.actioned_sigs == []

    async def test_proceeds_when_no_cross_agent_duplicate(self) -> None:
        github = AsyncMock()
        agent = SelfHealAgent(github=github, owner="o", repo="r", report_upstream=False)

        with (
            patch.object(
                agent,
                "_collect_failure_logs",
                AsyncMock(return_value=[("maintain", "some error log")]),
            ),
            patch.object(agent, "_get_existing_self_heal_sigs", AsyncMock(return_value=set())),
            patch.object(agent, "_run_id_already_tracked", AsyncMock(return_value=False)),
            patch(
                "caretaker.self_heal_agent.agent._classify_failure",
                return_value=(FailureKind.TRANSIENT, "Transient timeout", "retry later"),
            ),
        ):
            payload = {"workflow_run": {"id": 99999, "conclusion": "failure"}}
            report = await agent.run(event_payload=payload)

        # Transient failures still get analyzed but not actioned
        assert report.failures_analyzed == 1
        assert report.actioned_sigs == []


@pytest.mark.asyncio
class TestSelfHealCooldown:
    async def test_cooldown_skips_same_job_kind_within_window(self) -> None:
        from datetime import UTC, datetime

        recent_ts = datetime.now(UTC).isoformat()
        cooldowns = {"self-heal:maintain:config_error": recent_ts}

        github = AsyncMock()
        agent = SelfHealAgent(
            github=github,
            owner="o",
            repo="r",
            report_upstream=False,
            cooldown_hours=6,
            issue_cooldowns=cooldowns,
        )

        with (
            patch.object(
                agent,
                "_collect_failure_logs",
                AsyncMock(return_value=[("maintain", "pydantic ValidationError")]),
            ),
            patch.object(agent, "_get_existing_self_heal_sigs", AsyncMock(return_value=set())),
            patch.object(agent, "_run_id_already_tracked", AsyncMock(return_value=False)),
            patch(
                "caretaker.self_heal_agent.agent._classify_failure",
                return_value=(
                    FailureKind.CONFIG_ERROR,
                    "Config error in caretaker: pydantic ValidationError",
                    "details",
                ),
            ),
        ):
            report = await agent.run()

        assert report.local_issues_created == []
        assert report.actioned_sigs == []

    async def test_cooldown_allows_after_window_expires(self) -> None:
        from datetime import UTC, datetime, timedelta

        old_ts = (datetime.now(UTC) - timedelta(hours=24)).isoformat()
        cooldowns = {"self-heal:maintain:config_error": old_ts}

        github = AsyncMock()
        agent = SelfHealAgent(
            github=github,
            owner="o",
            repo="r",
            report_upstream=False,
            cooldown_hours=6,
            issue_cooldowns=cooldowns,
        )

        mock_issue = AsyncMock()
        mock_issue.number = 42

        with (
            patch.object(
                agent,
                "_collect_failure_logs",
                AsyncMock(return_value=[("maintain", "pydantic ValidationError")]),
            ),
            patch.object(agent, "_get_existing_self_heal_sigs", AsyncMock(return_value=set())),
            patch.object(agent, "_run_id_already_tracked", AsyncMock(return_value=False)),
            patch(
                "caretaker.self_heal_agent.agent._classify_failure",
                return_value=(
                    FailureKind.CONFIG_ERROR,
                    "Config error in caretaker: pydantic ValidationError",
                    "details",
                ),
            ),
            patch.object(agent, "_create_local_fix_issue", AsyncMock(return_value=mock_issue)),
        ):
            report = await agent.run()

        assert report.local_issues_created == [42]
        assert len(report.actioned_sigs) == 1
        assert "self-heal:maintain:config_error" in report.updated_cooldowns


@pytest.mark.asyncio
class TestSelfHealStormCap:
    """T-M7: storm cap keyed on ``(repo_slug, error_sig, hour_window)``.

    Prior behaviour counted all open self-heal issues in a repo regardless
    of signature; the first repo to hit a recurring failure burned the
    cap for every other signature. The new key bucket means a repeating
    error can burn its own cap without blocking unrelated failures.
    """

    @staticmethod
    def _stub_issue(number: int, created_at, sig: str = "abc123def456"):
        from caretaker.github_client.models import Issue, User

        return Issue(
            number=number,
            title="🩺 Caretaker self-heal: x",
            body=f"<!-- caretaker:self-heal --> sig:{sig} -->",
            state="open",
            user=User(login="bot", id=0, type="Bot"),
            created_at=created_at,
        )

    @staticmethod
    def _classified_sig() -> str:
        """Return the sig produced for the classification used in these tests."""
        from caretaker.self_heal_agent.agent import _sig

        return _sig("maintain", FailureKind.CONFIG_ERROR, "x")

    async def test_blocks_when_hourly_cap_hit_for_same_sig(self) -> None:
        from datetime import UTC, datetime, timedelta

        github = AsyncMock()
        agent = SelfHealAgent(
            github=github,
            owner="o",
            repo="r",
            report_upstream=False,
            max_open_per_hour=3,
            max_open_per_day=20,
        )
        incoming_sig = self._classified_sig()
        # Three issues already exist in the current hour window for the
        # *same* sig — the new incoming failure should be blocked. Use
        # seconds rather than minutes so the stubs can't land in the
        # previous hour bucket if wall-clock flips just after test start.
        now = datetime.now(UTC)
        recent_open = [
            self._stub_issue(i, now - timedelta(seconds=1), sig=incoming_sig) for i in range(3)
        ]

        with (
            patch.object(
                agent,
                "_collect_failure_logs",
                AsyncMock(return_value=[("maintain", "pydantic ValidationError")]),
            ),
            patch.object(agent, "_get_existing_self_heal_sigs", AsyncMock(return_value=set())),
            patch.object(agent, "_run_id_already_tracked", AsyncMock(return_value=False)),
            patch.object(agent._issues, "list", AsyncMock(return_value=recent_open)),
            patch(
                "caretaker.self_heal_agent.agent._classify_failure",
                return_value=(FailureKind.CONFIG_ERROR, "x", "details"),
            ),
            patch.object(agent, "_create_local_fix_issue", AsyncMock()) as create_mock,
        ):
            report = await agent.run()

        create_mock.assert_not_awaited()
        assert any("storm-cap" in e and "hourly cap hit" in e for e in report.errors)

    async def test_other_sigs_not_blocked_by_noisy_sig(self) -> None:
        """T-M7: a different error signature must not be starved out by
        a recurring-sig storm. Pre-cap implementations keyed on the raw
        label and blocked everything."""
        from datetime import UTC, datetime, timedelta

        github = AsyncMock()
        agent = SelfHealAgent(
            github=github,
            owner="o",
            repo="r",
            report_upstream=False,
            max_open_per_hour=3,
            max_open_per_day=20,
        )
        now = datetime.now(UTC)
        # 5 issues for SOME OTHER sig — well over the hourly cap — but
        # the incoming failure's sig is distinct so it must get through.
        noisy_open = [
            self._stub_issue(i, now - timedelta(minutes=5), sig="deadbeefcafe") for i in range(5)
        ]

        mock_issue = AsyncMock()
        mock_issue.number = 777
        with (
            patch.object(
                agent,
                "_collect_failure_logs",
                AsyncMock(return_value=[("maintain", "pydantic ValidationError")]),
            ),
            patch.object(agent, "_get_existing_self_heal_sigs", AsyncMock(return_value=set())),
            patch.object(agent, "_run_id_already_tracked", AsyncMock(return_value=False)),
            patch.object(agent._issues, "list", AsyncMock(return_value=noisy_open)),
            patch(
                "caretaker.self_heal_agent.agent._classify_failure",
                return_value=(FailureKind.CONFIG_ERROR, "x", "details"),
            ),
            patch.object(agent, "_create_local_fix_issue", AsyncMock(return_value=mock_issue)),
        ):
            report = await agent.run()

        assert report.local_issues_created == [777]

    async def test_burst_of_10_caps_at_5(self) -> None:
        """T-M7: simulate 10 identical failures in ~30 seconds; cap holds at 5.

        We file them serially through the same repo-scoped agent; only
        the first 5 should create issues — subsequent invocations see
        the cap hit and short-circuit.
        """
        from datetime import UTC, datetime, timedelta

        github = AsyncMock()
        agent = SelfHealAgent(
            github=github,
            owner="o",
            repo="r",
            report_upstream=False,
            max_open_per_hour=5,
            max_open_per_day=20,
            cooldown_hours=0,  # under test: storm cap only, coarse cooldown disabled
        )

        now = datetime.now(UTC)
        incoming_sig = self._classified_sig()
        existing: list[Any] = []
        created_count = 0
        stub_issue = self._stub_issue

        async def _list_issues(**_kwargs: Any) -> list[Any]:
            return list(existing)

        async def _create(*_args: Any, **_kwargs: Any) -> Any:
            nonlocal created_count
            created_count += 1
            mi = AsyncMock()
            mi.number = 1000 + created_count
            existing.append(
                stub_issue(mi.number, now - timedelta(seconds=created_count * 3), sig=incoming_sig)
            )
            return mi

        for _ in range(10):
            with (
                patch.object(
                    agent,
                    "_collect_failure_logs",
                    AsyncMock(return_value=[("maintain", "pydantic ValidationError")]),
                ),
                patch.object(agent, "_get_existing_self_heal_sigs", AsyncMock(return_value=set())),
                patch.object(agent, "_run_id_already_tracked", AsyncMock(return_value=False)),
                patch.object(agent._issues, "list", AsyncMock(side_effect=_list_issues)),
                patch(
                    "caretaker.self_heal_agent.agent._classify_failure",
                    return_value=(FailureKind.CONFIG_ERROR, "x", "details"),
                ),
                patch.object(agent, "_create_local_fix_issue", AsyncMock(side_effect=_create)),
            ):
                await agent.run()

        assert created_count == 5

    async def test_per_repo_isolation(self) -> None:
        """T-M7: a storm in one repo must not cap the OTHER repo's budget.

        Two ``SelfHealAgent`` instances, each scoped to its own
        ``(owner, repo)``. The first repo is saturated (5 open for the
        shared sig); the second repo has none and should still open one.
        """
        from datetime import UTC, datetime, timedelta

        now = datetime.now(UTC)
        incoming_sig = self._classified_sig()

        # Repo A — saturated.
        github_a = AsyncMock()
        agent_a = SelfHealAgent(
            github=github_a,
            owner="o",
            repo="a",
            report_upstream=False,
            max_open_per_hour=5,
            max_open_per_day=20,
        )
        saturated = [
            self._stub_issue(i, now - timedelta(seconds=1), sig=incoming_sig) for i in range(5)
        ]

        with (
            patch.object(
                agent_a,
                "_collect_failure_logs",
                AsyncMock(return_value=[("maintain", "pydantic ValidationError")]),
            ),
            patch.object(agent_a, "_get_existing_self_heal_sigs", AsyncMock(return_value=set())),
            patch.object(agent_a, "_run_id_already_tracked", AsyncMock(return_value=False)),
            patch.object(agent_a._issues, "list", AsyncMock(return_value=saturated)),
            patch(
                "caretaker.self_heal_agent.agent._classify_failure",
                return_value=(FailureKind.CONFIG_ERROR, "x", "details"),
            ),
            patch.object(agent_a, "_create_local_fix_issue", AsyncMock()) as create_a,
        ):
            await agent_a.run()

        create_a.assert_not_awaited()

        # Repo B — empty budget; should open one.
        github_b = AsyncMock()
        agent_b = SelfHealAgent(
            github=github_b,
            owner="o",
            repo="b",
            report_upstream=False,
            max_open_per_hour=5,
            max_open_per_day=20,
        )
        mock_issue_b = AsyncMock()
        mock_issue_b.number = 42

        with (
            patch.object(
                agent_b,
                "_collect_failure_logs",
                AsyncMock(return_value=[("maintain", "pydantic ValidationError")]),
            ),
            patch.object(agent_b, "_get_existing_self_heal_sigs", AsyncMock(return_value=set())),
            patch.object(agent_b, "_run_id_already_tracked", AsyncMock(return_value=False)),
            patch.object(agent_b._issues, "list", AsyncMock(return_value=[])),
            patch(
                "caretaker.self_heal_agent.agent._classify_failure",
                return_value=(FailureKind.CONFIG_ERROR, "x", "details"),
            ),
            patch.object(agent_b, "_create_local_fix_issue", AsyncMock(return_value=mock_issue_b)),
        ):
            report_b = await agent_b.run()

        assert report_b.local_issues_created == [42]

    async def test_allows_when_below_caps(self) -> None:
        from datetime import UTC, datetime, timedelta

        github = AsyncMock()
        agent = SelfHealAgent(
            github=github,
            owner="o",
            repo="r",
            report_upstream=False,
            max_open_per_hour=5,
            max_open_per_day=20,
        )
        incoming_sig = self._classified_sig()
        # 2 existing issues for the same sig, under cap of 5.
        recent = [
            self._stub_issue(i, datetime.now(UTC) - timedelta(minutes=10), sig=incoming_sig)
            for i in range(2)
        ]

        mock_issue = AsyncMock()
        mock_issue.number = 99
        with (
            patch.object(
                agent,
                "_collect_failure_logs",
                AsyncMock(return_value=[("maintain", "pydantic ValidationError")]),
            ),
            patch.object(agent, "_get_existing_self_heal_sigs", AsyncMock(return_value=set())),
            patch.object(agent, "_run_id_already_tracked", AsyncMock(return_value=False)),
            patch.object(agent._issues, "list", AsyncMock(return_value=recent)),
            patch(
                "caretaker.self_heal_agent.agent._classify_failure",
                return_value=(FailureKind.CONFIG_ERROR, "x", "details"),
            ),
            patch.object(agent, "_create_local_fix_issue", AsyncMock(return_value=mock_issue)),
        ):
            report = await agent.run()

        assert report.local_issues_created == [99]

    async def test_caps_disabled_when_zero(self) -> None:
        from datetime import UTC, datetime, timedelta

        github = AsyncMock()
        agent = SelfHealAgent(
            github=github,
            owner="o",
            repo="r",
            report_upstream=False,
            max_open_per_hour=0,  # disabled
            max_open_per_day=0,  # disabled
        )
        incoming_sig = self._classified_sig()
        # 100 issues opened in the last hour for the SAME sig — cap disabled, should still allow
        recent = [
            self._stub_issue(i, datetime.now(UTC) - timedelta(minutes=1), sig=incoming_sig)
            for i in range(100)
        ]

        mock_issue = AsyncMock()
        mock_issue.number = 1
        with (
            patch.object(
                agent,
                "_collect_failure_logs",
                AsyncMock(return_value=[("maintain", "pydantic ValidationError")]),
            ),
            patch.object(agent, "_get_existing_self_heal_sigs", AsyncMock(return_value=set())),
            patch.object(agent, "_run_id_already_tracked", AsyncMock(return_value=False)),
            patch.object(agent._issues, "list", AsyncMock(return_value=recent)),
            patch(
                "caretaker.self_heal_agent.agent._classify_failure",
                return_value=(FailureKind.CONFIG_ERROR, "x", "details"),
            ),
            patch.object(agent, "_create_local_fix_issue", AsyncMock(return_value=mock_issue)),
        ):
            report = await agent.run()

        assert report.local_issues_created == [1]


class TestClassifyFailureTrackingIssueFull:
    """Failures caused by the 2500-comment limit should be classified as CONFIG_ERROR."""

    _LOG_TEMPLATE = (
        "2026-04-20T07:59:54Z INFO  caretaker.orchestrator — Handling event: workflow_run\n"
        "2026-04-20T07:59:54Z INFO  httpx — HTTP Request: POST "
        "https://api.github.com/repos/owner/repo/issues/1/comments "
        '"HTTP/1.1 403 Forbidden"\n'
        "2026-04-20T07:59:54Z Traceback (most recent call last):\n"
        "  File ..., in save\n"
        "    raise GitHubAPIError(resp.status_code, resp.text)\n"
        "caretaker.github_client.api.GitHubAPIError: GitHub API error 403: "
        '{{"message":"Commenting is disabled on issues with more than 2500 comments",'
        '"documentation_url":"https://docs.github.com/rest","status":"403"}}\n'
        "2026-04-20T07:59:54Z ##[error]Process completed with exit code 1.\n"
    )

    def test_classifies_as_config_error(self) -> None:
        kind, title, details = _classify_failure("maintain", self._LOG_TEMPLATE)
        assert kind == FailureKind.CONFIG_ERROR

    def test_title_mentions_comment_limit(self) -> None:
        _, title, _ = _classify_failure("maintain", self._LOG_TEMPLATE)
        assert "comment" in title.lower() or "limit" in title.lower()

    def test_not_classified_as_unknown(self) -> None:
        kind, _, _ = _classify_failure("maintain", self._LOG_TEMPLATE)
        assert kind != FailureKind.UNKNOWN

    def test_message_fragment_only(self) -> None:
        """The bare message fragment alone is enough to trigger the classifier."""
        log = "Commenting is disabled on issues with more than 2500 comments\n"
        kind, _, _ = _classify_failure("maintain", log)
        assert kind == FailureKind.CONFIG_ERROR


class TestExtractErrorContext:
    """_extract_error_context should find the ##[error] marker in the full log."""

    def test_returns_context_around_error_marker(self) -> None:
        preamble = "\n".join(f"2026-04-14T23:22:{i:02d}Z setup line {i}" for i in range(30))
        error_line = "2026-04-14T23:23:00Z ##[error]Process completed with exit code 1."
        cleanup = "\n".join(
            [
                "2026-04-14T23:23:01Z Post job cleanup.",
                "2026-04-14T23:23:02Z git config --unset ...",
                "2026-04-14T23:23:03Z Cleaning up orphan processes",
                "2026-04-14T23:23:04Z HOME=/home/runner",
            ]
        )
        log = f"{preamble}\n{error_line}\n{cleanup}"

        context = _extract_error_context(log)

        # The ##[error] line must be present in the returned context.
        assert "##[error]" in context
        # Lines well before the error (setup context) should be included.
        assert "setup line" in context

    def test_error_marker_before_cleanup_is_included(self) -> None:
        """The ##[error] line should be in the context even when buried before cleanup."""
        caretaker_lines = "\n".join(
            f"2026-04-14T23:22:{i:02d}Z caretaker output line {i}" for i in range(20)
        )
        error_line = "2026-04-14T23:22:50Z ##[error]Process completed with exit code 1."
        # Simulate a very long cleanup section that would dominate log_text[-4000:]
        cleanup = (
            "\n".join(f"2026-04-14T23:22:5{i}Z Post job cleanup step {i}" for i in range(9))
            + "\n"
            + "2026-04-14T23:23:00Z Cleaning up orphan processes\n" * 50
        )
        log = f"{caretaker_lines}\n{error_line}\n{cleanup}"

        context = _extract_error_context(log)

        assert "##[error]" in context

    def test_fallback_strips_cleanup_lines_when_no_error_marker(self) -> None:
        log = (
            "2026-04-14T23:22:00Z Meaningful log line\n"
            "2026-04-14T23:22:01Z Post job cleanup.\n"
            "2026-04-14T23:22:02Z git config --unset token\n"
            "2026-04-14T23:22:03Z Cleaning up orphan processes\n"
        )
        context = _extract_error_context(log)
        assert "Meaningful log line" in context
        assert "Post job cleanup" not in context

    def test_uses_last_error_marker_when_multiple_present(self) -> None:
        log = (
            "2026-04-14T23:22:00Z ##[error]First error\n"
            "2026-04-14T23:22:10Z Some recovery attempt\n"
            "2026-04-14T23:22:20Z ##[error]Second error\n"
            "2026-04-14T23:22:21Z Post job cleanup.\n"
        )
        context = _extract_error_context(log)
        assert "Second error" in context


class TestExtractFailingStep:
    """_extract_failing_step should return the step name wrapping the ##[error]."""

    def test_returns_step_name_before_error(self) -> None:
        log = (
            "2026-04-14T23:22:00Z ##[group]Run caretaker maintain\n"
            "2026-04-14T23:22:01Z Some output\n"
            "2026-04-14T23:22:02Z ##[error]Process completed with exit code 1.\n"
            "2026-04-14T23:22:03Z ##[endgroup]\n"
        )
        step = _extract_failing_step(log)
        assert step == "Run caretaker maintain"

    def test_returns_none_when_no_error_marker(self) -> None:
        log = "2026-04-14T23:22:00Z ##[group]Run tests\nAll passed.\n"
        assert _extract_failing_step(log) is None

    def test_returns_none_when_no_group_before_error(self) -> None:
        log = "2026-04-14T23:22:00Z ##[error]Process completed with exit code 1.\n"
        assert _extract_failing_step(log) is None

    def test_picks_nearest_group_header(self) -> None:
        log = (
            "2026-04-14T23:22:00Z ##[group]Set up job\n"
            "2026-04-14T23:22:01Z ##[endgroup]\n"
            "2026-04-14T23:22:02Z ##[group]Run pytest\n"
            "2026-04-14T23:22:03Z FAILED tests/test_x.py\n"
            "2026-04-14T23:22:04Z ##[error]Process completed with exit code 1.\n"
        )
        step = _extract_failing_step(log)
        assert step == "Run pytest"

    def test_strips_timestamp_prefix(self) -> None:
        log = (
            "2026-04-14T23:22:00Z ##[group]Run caretaker maintain\n"
            "2026-04-14T23:22:01Z ##[error]exit code 1.\n"
        )
        step = _extract_failing_step(log)
        assert step is not None
        assert "2026-04-14" not in step


class TestCleanLogSnippet:
    """_clean_log_snippet should strip cleanup lines and return last n useful lines."""

    def test_strips_post_job_cleanup(self) -> None:
        log = (
            "2026-04-14T23:22:00Z Real error output\n"
            "2026-04-14T23:22:01Z Post job cleanup.\n"
            "2026-04-14T23:22:02Z git config --unset token\n"
            "2026-04-14T23:22:03Z Cleaning up orphan processes\n"
            "2026-04-14T23:22:04Z HOME=/home/runner RUNNER_TOOL=...\n"
        )
        snippet = _clean_log_snippet(log, n=30)
        assert "Real error output" in snippet
        assert "Post job cleanup" not in snippet
        assert "git config --unset" not in snippet
        assert "Cleaning up orphan" not in snippet

    def test_returns_last_n_lines(self) -> None:
        lines = [f"2026-04-14T23:22:{i:02d}Z line {i}" for i in range(60)]
        log = "\n".join(lines)
        snippet = _clean_log_snippet(log, n=10)
        # Last 10 lines are indices 50-59
        assert "line 59" in snippet
        assert "line 50" in snippet
        assert "line 49" not in snippet

    def test_falls_back_to_raw_lines_when_all_cleaned(self) -> None:
        """If every line matches cleanup pattern, return last n raw lines."""
        log = "\n".join(
            [
                "Post job cleanup.",
                "git config --unset token",
                "Cleaning up orphan processes",
            ]
        )
        snippet = _clean_log_snippet(log, n=30)
        # Should not crash; must return something
        assert len(snippet) > 0


@pytest.mark.asyncio
class TestSelfHealDedup:
    """_get_existing_self_heal_sigs should also check recently closed issues."""

    async def test_deduplicates_against_recently_closed_issue(self) -> None:
        from datetime import UTC, timedelta
        from datetime import datetime as _dt
        from unittest.mock import AsyncMock

        from caretaker.github_client.models import Issue, User

        github = AsyncMock()
        agent = SelfHealAgent(github=github, owner="o", repo="r", report_upstream=False)

        recently_closed = Issue(
            number=10,
            title="🩺 x",
            body="<!-- caretaker:self-heal --> sig:aabbccddeeff -->",
            state="closed",
            user=User(login="bot", id=0, type="Bot"),
            created_at=_dt.now(UTC) - timedelta(hours=1),
            closed_at=_dt.now(UTC) - timedelta(minutes=30),
        )
        old_closed = Issue(
            number=11,
            title="🩺 x",
            body="<!-- caretaker:self-heal --> sig:112233445566 -->",
            state="closed",
            user=User(login="bot", id=0, type="Bot"),
            created_at=_dt.now(UTC) - timedelta(days=7),
            closed_at=_dt.now(UTC) - timedelta(days=7),
        )

        async def mock_list(state: str = "open", **_kw: Any) -> list[Issue]:
            if state == "open":
                return []
            return [recently_closed, old_closed]

        with patch.object(agent._issues, "list", side_effect=mock_list):
            sigs = await agent._get_existing_self_heal_sigs()

        assert "aabbccddeeff" in sigs, "recently-closed issue sig should be deduped"
        assert "112233445566" not in sigs, "old closed issue sig should not be deduped"
