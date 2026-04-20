"""Tests for self-heal failure classification."""

import gzip
import io
import zipfile
from unittest.mock import AsyncMock, patch

import pytest

from caretaker.self_heal_agent.agent import (
    FailureKind,
    SelfHealAgent,
    _classify_failure,
    _decode_job_log_payload,
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
    """Sprint 2 C4: refuse to open new self-heal issues during a storm.

    Catches the F1 retry-storm pattern (caretaker-self 2026-04-14: 108 PRs in
    90 minutes). Counts existing open self-heal issues by createdAt timestamp
    so the cap survives across workflow runs.
    """

    @staticmethod
    def _stub_issue(number: int, created_at):
        from caretaker.github_client.models import Issue, User

        return Issue(
            number=number,
            title="🩺 Caretaker self-heal: x",
            body="<!-- caretaker:self-heal --> sig:abc123def456 -->",
            state="open",
            user=User(login="bot", id=0, type="Bot"),
            created_at=created_at,
        )

    async def test_blocks_when_hourly_cap_hit(self) -> None:
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
        recent_open = [
            self._stub_issue(i, datetime.now(UTC) - timedelta(minutes=10))
            for i in range(3)  # exactly the hourly cap
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
            patch.object(agent, "_create_local_fix_issue", AsyncMock()) as create_mock,
        ):
            report = await agent.run()

        create_mock.assert_not_awaited()
        assert any("storm-cap" in e and "hourly cap hit" in e for e in report.errors)

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
        # 2 in last hour (under cap of 5)
        recent = [self._stub_issue(i, datetime.now(UTC) - timedelta(minutes=10)) for i in range(2)]

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
        # 100 issues opened in the last hour — cap disabled, should still allow
        recent = [self._stub_issue(i, datetime.now(UTC) - timedelta(minutes=1)) for i in range(100)]

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
