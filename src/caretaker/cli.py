"""CLI entrypoint for caretaker."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

import click

from caretaker.config import MaintainerConfig
from caretaker.orchestrator import Orchestrator

if TYPE_CHECKING:
    from datetime import datetime


def _load_dotenv_if_present() -> None:
    """Populate ``os.environ`` from a ``.env`` file in the working directory.

    No-op if the file is missing.  Existing shell variables win over .env
    values so CI/production deployments can always override.
    """
    env_path = Path(os.environ.get("CARETAKER_DOTENV", ".env"))
    if not env_path.is_file():
        return
    for raw in env_path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv_if_present()


class RunMode(StrEnum):
    FULL = "full"
    PR_ONLY = "pr-only"
    ISSUE_ONLY = "issue-only"
    UPGRADE_ONLY = "upgrade"
    DEVOPS = "devops"
    SECURITY = "security"
    DEPENDENCIES = "deps"
    DOCS = "docs"
    CHARLIE = "charlie"
    STALE = "stale"
    ESCALATION = "escalation"
    SELF_HEAL = "self-heal"
    PRINCIPAL = "principal"
    TEST = "test"
    REFACTOR = "refactor"
    PERF = "perf"
    MIGRATION = "migration"
    DRY_RUN = "dry-run"
    EVENT = "event"


def _configure_logging(log_file: str | None, debug: bool) -> None:
    """Configure root logger with console and optional file handlers."""
    level = logging.DEBUG if debug else logging.INFO
    fmt = "%(asctime)s %(levelname)-8s %(name)s — %(message)s"
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if log_file:
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)  # always capture debug in the file
        file_handler.setFormatter(logging.Formatter(fmt))
        handlers.append(file_handler)
    logging.basicConfig(level=level, format=fmt, handlers=handlers, force=True)


@click.group()
@click.version_option()
def main() -> None:
    """Caretaker — autonomous repo management."""


@main.command()
@click.option("--config", required=True, type=click.Path(exists=True), help="Path to config.yml")
@click.option(
    "--mode",
    type=click.Choice([m.value for m in RunMode]),
    default=RunMode.FULL.value,
    help="Run mode",
)
@click.option("--event-type", default=None, help="GitHub event type (push, pull_request, etc.)")
@click.option("--event-payload", default=None, help="JSON-encoded GitHub event payload")
@click.option("--dry-run", is_flag=True, default=False, help="Read-only mode")
@click.option(
    "--log-file",
    default=None,
    envvar="CARETAKER_LOG_FILE",
    help="Write full DEBUG-level log to this file (uploaded as a CI artifact).",
)
@click.option(
    "--report-file",
    default=None,
    envvar="CARETAKER_REPORT_FILE",
    help="Write a JSON run-report to this file (uploaded as a CI artifact).",
)
@click.option("--debug", is_flag=True, default=False, help="Enable DEBUG logging to stderr.")
def run(
    config: str,
    mode: str,
    event_type: str | None,
    event_payload: str | None,
    dry_run: bool,
    log_file: str | None,
    report_file: str | None,
    debug: bool,
) -> None:
    """Run the maintainer orchestrator."""
    _configure_logging(log_file, debug)

    parsed_mode = RunMode(mode)
    if dry_run:
        parsed_mode = RunMode.DRY_RUN

    payload = None
    if event_payload:
        payload = json.loads(event_payload)

    orchestrator = Orchestrator.from_config_path(config)
    exit_code = asyncio.run(
        orchestrator.run(
            mode=parsed_mode,
            event_type=event_type,
            event_payload=payload,
            report_path=report_file,
        )
    )
    sys.exit(exit_code)


@main.command("validate-config")
@click.option("--config", required=True, type=click.Path(exists=True), help="Path to config.yml")
def validate_config(config: str) -> None:
    """Validate maintainer config file and exit."""
    try:
        loaded = MaintainerConfig.from_yaml(config)
    except Exception as exc:  # pragma: no cover - surfaced to CLI user
        click.echo(f"❌ Invalid config: {exc}", err=True)
        raise SystemExit(1) from exc

    click.echo(
        f"✅ Config valid (version={loaded.version}, schedule={loaded.orchestrator.schedule})"
    )


DEFAULT_DOCTOR_CONFIG = ".github/maintainer/config.yml"


@main.command("doctor")
@click.option(
    "--config",
    default=DEFAULT_DOCTOR_CONFIG,
    show_default=True,
    type=click.Path(),
    help="Path to config.yml. Defaults to .github/maintainer/config.yml.",
)
@click.option(
    "--json",
    "emit_json",
    is_flag=True,
    default=False,
    help="Emit a machine-readable JSON summary on stdout (human table still goes to stderr).",
)
@click.option(
    "--strict",
    is_flag=True,
    default=False,
    help="Treat unreachable external services as FAIL instead of WARN.",
)
@click.option(
    "--skip-github",
    is_flag=True,
    default=False,
    help="Skip GitHub token probes (used in tests / offline environments).",
)
def doctor(config: str, emit_json: bool, strict: bool, skip_github: bool) -> None:
    """Preflight every required secret, scope, and external service.

    Exit code matrix:

    * 0 — no FAILs
    * 1 — at least one FAIL
    * 2 — internal error (preflight itself crashed)
    """
    # Imports are deferred so ``caretaker --help`` stays fast and test
    # fixtures that stub the CLI entrypoint don't pay for httpx + pydantic.
    import traceback

    from caretaker.doctor import Severity, render_table, run_doctor_sync

    try:
        loaded = MaintainerConfig.from_yaml(config)
    except FileNotFoundError as exc:
        click.echo(f"doctor: config file not found: {exc}", err=True)
        raise SystemExit(2) from exc
    except Exception as exc:
        click.echo(f"doctor: failed to load config: {exc}", err=True)
        raise SystemExit(2) from exc

    try:
        report = run_doctor_sync(loaded, strict=strict, skip_github=skip_github)
    except Exception as exc:  # pragma: no cover - surfaced to CLI user
        click.echo(f"doctor: internal error during preflight: {exc}", err=True)
        click.echo(traceback.format_exc(), err=True)
        raise SystemExit(2) from exc

    # Human-readable table → stderr so the optional JSON payload on
    # stdout stays cleanly parseable for CI consumers.
    click.echo(render_table(report), err=True)

    if emit_json:
        click.echo(json.dumps(report.to_dict(), indent=2, sort_keys=True))

    if any(r.severity is Severity.FAIL for r in report.results):
        raise SystemExit(1)


@main.group("eval")
def eval_group() -> None:
    """Shadow-decision evaluation harness (workstream A4).

    Runs the per-site scorer registry over ``:ShadowDecision`` records
    and (when ``BRAINTRUST_API_KEY`` is set and the ``eval`` extra is
    installed) uploads one experiment per site. Use ``--dry-run`` to
    emit a JSON report locally without calling Braintrust.
    """


def _parse_since(value: str) -> datetime:
    """Parse ``--since`` as either ``<N>[smhd]`` or an ISO-8601 timestamp.

    Kept module-local (not a Click type) because the duration grammar is
    tiny and reusing click's DateTime type would lose the ``7d`` case.
    """
    from datetime import UTC, datetime, timedelta

    stripped = value.strip()
    if stripped and stripped[-1] in {"s", "m", "h", "d"} and stripped[:-1].isdigit():
        amount = int(stripped[:-1])
        unit = stripped[-1]
        delta = {
            "s": timedelta(seconds=amount),
            "m": timedelta(minutes=amount),
            "h": timedelta(hours=amount),
            "d": timedelta(days=amount),
        }[unit]
        return datetime.now(UTC) - delta
    try:
        parsed = datetime.fromisoformat(stripped)
    except ValueError as exc:
        raise click.BadParameter(
            f"--since must be a duration like '24h' or an ISO-8601 timestamp; got {value!r}"
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


@eval_group.command("run")
@click.option(
    "--since",
    default="24h",
    show_default=True,
    help="Duration (e.g. ``24h``, ``7d``) or ISO-8601 timestamp for the lower window bound.",
)
@click.option(
    "--sites",
    default=None,
    help="Comma-separated list of decision sites (default: every registered site).",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Emit a local JSON report without calling Braintrust.",
)
@click.option(
    "--output",
    default=None,
    type=click.Path(),
    help="Write the JSON report to this path instead of stdout.",
)
def eval_run(
    since: str,
    sites: str | None,
    dry_run: bool,
    output: str | None,
) -> None:
    """Run the nightly shadow-decision eval harness."""
    from datetime import UTC, datetime

    from caretaker.eval import get_default_client, run_nightly_eval

    since_dt = _parse_since(since)
    until_dt = datetime.now(UTC)
    site_list = [s.strip() for s in sites.split(",") if s.strip()] if sites else None

    braintrust_client = None if dry_run else get_default_client()

    report = run_nightly_eval(
        since=since_dt,
        until=until_dt,
        sites=site_list,
        braintrust_client=braintrust_client,
        dry_run=dry_run,
    )

    payload = json.dumps(report.to_dict(), indent=2, sort_keys=True)
    if output:
        Path(output).write_text(payload + "\n")
    else:
        click.echo(payload)


if __name__ == "__main__":
    main()
