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
            patch(
                "caretaker.self_heal_agent.agent._classify_failure",
                return_value=(FailureKind.TRANSIENT, "Transient timeout", "retry later"),
            ),
        ):
            report = await agent.run()

        assert report.actioned_sigs == []
