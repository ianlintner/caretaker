#!/usr/bin/env python3
"""Enforce-gate checker for ``shadow → enforce`` promotion PRs.

Invoked by ``.github/workflows/enforce-gate.yml`` with paths to the
base (``main``) and head (PR) caretaker config YAMLs. Exits 0 when no
flip is found or every flipped site clears its
``enforce_gate.min_agreement_rate`` floor; exits 1 otherwise, with a
human-readable diagnostic on stderr.

Fails-closed when the :mod:`caretaker.eval.store` has no recent data —
the CI runner is always fresh, so the workflow is expected to first
materialise a report via ``caretaker eval run --since 7d`` before this
script runs.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from caretaker.config import MaintainerConfig
from caretaker.eval import gate


def _load_config(path: str) -> MaintainerConfig:
    """Load a caretaker config from a YAML file."""
    p = Path(path)
    if not p.is_file():
        raise SystemExit(f"check_enforce_gate: config file not found: {p}")
    return MaintainerConfig.from_yaml(p)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True, help="Path to base config (main branch).")
    parser.add_argument("--head", required=True, help="Path to head config (PR).")
    parser.add_argument(
        "--window-days",
        type=int,
        default=7,
        help="Rolling window (days) for the agreement-rate check. Defaults to 7.",
    )
    parser.add_argument(
        "--emit-json",
        action="store_true",
        help="Print the gate decisions as JSON on stdout.",
    )
    parser.add_argument(
        "--eval-report",
        default=None,
        help=(
            "Optional path to a JSON nightly report; when given, seeds the eval "
            "store so the gate has history to read from without depending on a "
            "long-lived process. Consumed by CI where the harness runs in one "
            "step and the gate runs in another."
        ),
    )
    args = parser.parse_args(argv)

    if args.eval_report:
        _seed_store_from_json(args.eval_report)

    base_cfg = _load_config(args.base)
    head_cfg = _load_config(args.head)

    decisions = gate.check_all(base_cfg, head_cfg, window_days=args.window_days)

    if args.emit_json:
        print(json.dumps([d.to_dict() for d in decisions], indent=2, sort_keys=True))

    failing = [d for d in decisions if not d.passed]
    if not decisions:
        print("check_enforce_gate: no shadow→enforce flips in this PR; nothing to gate.")
        return 0
    if not failing:
        print("check_enforce_gate: all flipped sites cleared the agreement-rate gate.")
        for d in decisions:
            print(f"  ✓ {d.site}: {d.reason}")
        return 0

    print("check_enforce_gate: enforce-gate FAILED for:", file=sys.stderr)
    for d in failing:
        print(f"  ✗ {d.site}: {d.reason}", file=sys.stderr)
    return 1


def _seed_store_from_json(path: str) -> None:
    """Materialise a :class:`NightlyReport` from a JSON file into the store.

    Minimal shape: only the fields the gate actually reads are
    required. We intentionally don't round-trip every scorer summary so
    CI does not have to keep the JSON schema perfectly in sync with the
    in-memory report.
    """
    from datetime import datetime

    from caretaker.eval import store
    from caretaker.eval.harness import NightlyReport, ScorerSummary, SiteReport

    data = json.loads(Path(path).read_text())
    since = datetime.fromisoformat(data["since"])
    until = datetime.fromisoformat(data["until"])
    sites: list[SiteReport] = []
    for s in data.get("sites", []):
        sites.append(
            SiteReport(
                site=s["site"],
                record_count=int(s.get("record_count", 0)),
                scorer_summaries=[
                    ScorerSummary(
                        scorer=ss["scorer"],
                        mean=float(ss["mean"]),
                        count=int(ss.get("count", 0)),
                        judge_disagreements=int(ss.get("judge_disagreements", 0)),
                    )
                    for ss in s.get("scorer_summaries", [])
                ],
                experiment_url=s.get("experiment_url"),
                braintrust_logged=bool(s.get("braintrust_logged", False)),
            )
        )
    report = NightlyReport(since=since, until=until, sites=sites)
    store.store_report(report)


if __name__ == "__main__":
    sys.exit(main())
