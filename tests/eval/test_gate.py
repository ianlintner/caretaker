"""Tests for :mod:`caretaker.eval.gate` and the CLI wrapper."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from caretaker.config import MaintainerConfig
from caretaker.eval import gate, store
from caretaker.eval.harness import NightlyReport, ScorerSummary, SiteReport


@pytest.fixture(autouse=True)
def _reset_store() -> None:
    store.clear_for_tests()
    yield
    store.clear_for_tests()


def _make_cfg(mode: str, *, min_rate: float = 0.95) -> MaintainerConfig:
    return MaintainerConfig.model_validate(
        {
            "version": "v1",
            "agentic": {
                "readiness": {
                    "mode": mode,
                    "enforce_gate": {"min_agreement_rate": min_rate},
                }
            },
        }
    )


def _seed_history(site: str, rate: float) -> None:
    report = NightlyReport(
        since=datetime.now(UTC) - timedelta(days=1),
        until=datetime.now(UTC),
        sites=[
            SiteReport(
                site=site,
                record_count=50,
                scorer_summaries=[
                    ScorerSummary(scorer="m", mean=rate, count=50),
                ],
                experiment_url=None,
                braintrust_logged=False,
            ),
        ],
    )
    store.store_report(report)


class TestFindFlippedSites:
    def test_shadow_to_enforce_is_flagged(self) -> None:
        flipped = gate.find_flipped_sites(_make_cfg("shadow"), _make_cfg("enforce"))
        assert flipped == ["readiness"]

    def test_off_to_shadow_is_not_flagged(self) -> None:
        flipped = gate.find_flipped_sites(_make_cfg("off"), _make_cfg("shadow"))
        assert flipped == []

    def test_enforce_to_shadow_is_deescalation_and_skipped(self) -> None:
        flipped = gate.find_flipped_sites(_make_cfg("enforce"), _make_cfg("shadow"))
        assert flipped == []


class TestEvaluateGate:
    def test_passes_when_history_clears_floor(self) -> None:
        _seed_history("readiness", rate=0.97)
        decision = gate.evaluate_gate("readiness", _make_cfg("enforce", min_rate=0.95))
        assert decision.passed is True
        assert decision.observed_rate == pytest.approx(0.97)
        assert decision.required_rate == pytest.approx(0.95)

    def test_fails_when_history_below_floor(self) -> None:
        _seed_history("readiness", rate=0.80)
        decision = gate.evaluate_gate("readiness", _make_cfg("enforce", min_rate=0.95))
        assert decision.passed is False
        assert decision.observed_rate == pytest.approx(0.80)
        assert "< required" in decision.reason

    def test_fails_closed_without_history(self) -> None:
        decision = gate.evaluate_gate("readiness", _make_cfg("enforce"))
        assert decision.passed is False
        assert decision.observed_rate is None
        assert "no 7d eval history" in decision.reason


class TestCheckEnforceGateScript:
    """Smoke tests for the CLI wrapper used by the GitHub Actions workflow."""

    def _script(self) -> Path:
        # Workspace path — the script is invoked by CI via ``python <path>``.
        root = Path(__file__).resolve().parents[2]
        return root / "scripts" / "check_enforce_gate.py"

    def _write_cfg(self, tmp: Path, name: str, mode: str) -> Path:
        data = {
            "version": "v1",
            "agentic": {
                "readiness": {
                    "mode": mode,
                    "enforce_gate": {"min_agreement_rate": 0.95},
                },
            },
        }
        p = tmp / f"{name}.yml"
        p.write_text(yaml.safe_dump(data))
        return p

    def _write_report(self, tmp: Path, rate: float) -> Path:
        until = datetime.now(UTC)
        since = until - timedelta(days=1)
        payload = {
            "since": since.isoformat(),
            "until": until.isoformat(),
            "generated_at": until.isoformat(),
            "sites": [
                {
                    "site": "readiness",
                    "record_count": 50,
                    "agreement_rate": rate,
                    "scorer_summaries": [
                        {
                            "scorer": "readiness_verdict_match",
                            "mean": rate,
                            "count": 50,
                            "judge_disagreements": 0,
                        }
                    ],
                    "experiment_url": None,
                    "braintrust_logged": False,
                }
            ],
        }
        p = tmp / "report.json"
        p.write_text(json.dumps(payload))
        return p

    def test_fails_closed_when_no_report_and_pr_flips_mode(self, tmp_path: Path) -> None:
        base = self._write_cfg(tmp_path, "base", "shadow")
        head = self._write_cfg(tmp_path, "head", "enforce")
        result = subprocess.run(
            [
                sys.executable,
                str(self._script()),
                "--base",
                str(base),
                "--head",
                str(head),
                "--emit-json",
            ],
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src")},
        )
        assert result.returncode == 1
        assert "fail-closed" in (result.stdout + result.stderr)

    def test_passes_with_report_above_threshold(self, tmp_path: Path) -> None:
        base = self._write_cfg(tmp_path, "base", "shadow")
        head = self._write_cfg(tmp_path, "head", "enforce")
        report = self._write_report(tmp_path, rate=0.99)
        result = subprocess.run(
            [
                sys.executable,
                str(self._script()),
                "--base",
                str(base),
                "--head",
                str(head),
                "--eval-report",
                str(report),
                "--emit-json",
            ],
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src")},
        )
        assert result.returncode == 0, result.stderr
        # Script prints a JSON array followed by a human "all cleared" line.
        # Slice to the first top-level ``]`` so both halves stay parseable.
        end = result.stdout.index("]")
        decisions = json.loads(result.stdout[: end + 1])
        assert decisions[0]["passed"] is True

    def test_no_flip_in_pr_is_a_noop(self, tmp_path: Path) -> None:
        base = self._write_cfg(tmp_path, "base", "shadow")
        head = self._write_cfg(tmp_path, "head", "shadow")
        result = subprocess.run(
            [
                sys.executable,
                str(self._script()),
                "--base",
                str(base),
                "--head",
                str(head),
            ],
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src")},
        )
        assert result.returncode == 0
        assert "nothing to gate" in result.stdout
