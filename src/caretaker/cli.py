"""CLI entrypoint for caretaker."""

from __future__ import annotations

import asyncio
import contextlib
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
    SHEPHERD = "shepherd"
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
@click.option(
    "--bootstrap-check",
    is_flag=True,
    default=False,
    help=(
        "Run only the offline bootstrap preflight: import caretaker, parse "
        "config.yml, read the pinned version file, and check env vars for "
        "every enabled agent. No GitHub or network calls. Intended to run "
        "as the first step of a consumer workflow, before the full doctor."
    ),
)
@click.option(
    "--llm-probe",
    "llm_probe",
    is_flag=True,
    default=False,
    help=(
        "Run only the online LLM-endpoint probe: resolve every distinct "
        "model string (default_model, feature_models[*].model, "
        "fallback_models) and ping each with a cheap 1-token "
        "litellm.acompletion call to confirm the endpoint is live. "
        "Catches typos in model names, missing/rotated API keys, wrong "
        "region/deployment ids, and unknown Azure deployments. Spends a "
        "handful of tokens per run (tiny but nonzero cost) so run it "
        "once on onboarding or after rotating keys, not on every commit. "
        "Only runs the LLM-probe checks; for full preflight use "
        "--bootstrap-check or the default. --bootstrap-check is offline "
        "and fast (parses config, reads env, validates secrets); "
        "--llm-probe is online and spends a handful of tokens."
    ),
)
@click.option(
    "--pin-path",
    default=None,
    type=click.Path(),
    help=(
        "Path to the caretaker version pin file. "
        "Defaults to .github/maintainer/.version. "
        "Only read in --bootstrap-check mode."
    ),
)
def doctor(
    config: str,
    emit_json: bool,
    strict: bool,
    skip_github: bool,
    bootstrap_check: bool,
    llm_probe: bool,
    pin_path: str | None,
) -> None:
    """Preflight every required secret, scope, and external service.

    Exit code matrix:

    * 0 — no FAILs
    * 1 — at least one FAIL
    * 2 — internal error (preflight itself crashed)

    Flag modes (mutually exclusive with the default full run):

    * --bootstrap-check — offline, fast: parses config, reads env vars,
      validates the version-pin file. No network calls. Wire this in as
      the first step of a consumer workflow so a bad pin or missing
      secret shows up as a readable FAIL row.
    * --llm-probe — online, ~1 paid token per configured model: resolves
      every distinct model in default_model / feature_models /
      fallback_models against the LiteLLM registry and pings each
      endpoint. Catches model typos, wrong/missing API keys, and
      unknown Azure deployments that otherwise stay invisible until
      the first feature fires. Run once on onboarding or after
      rotating keys.

    CI recipe: chain ``caretaker doctor && caretaker doctor --llm-probe``
    to get both the offline and online preflights in sequence.
    """
    # Imports are deferred so ``caretaker --help`` stays fast and test
    # fixtures that stub the CLI entrypoint don't pay for httpx + pydantic.
    import traceback

    from caretaker.doctor import (
        DEFAULT_VERSION_PIN_PATH,
        Severity,
        render_table,
        run_bootstrap_check,
        run_doctor_sync,
        run_llm_probe_sync,
    )

    # --bootstrap-check is the tight, offline preflight: it parses the
    # config itself rather than relying on the CLI's own load step so a
    # parse failure shows up as a FAIL *row* with a specific hint rather
    # than an exit-2 "internal error" that makes operators think the
    # preflight itself is broken. Consumer workflows wire this in as the
    # first step before the real doctor call.
    if bootstrap_check:
        try:
            report = run_bootstrap_check(
                config_path=config,
                pin_path=pin_path if pin_path is not None else DEFAULT_VERSION_PIN_PATH,
            )
        except Exception as exc:  # pragma: no cover - surfaced to CLI user
            click.echo(f"doctor: internal error during bootstrap-check: {exc}", err=True)
            click.echo(traceback.format_exc(), err=True)
            raise SystemExit(2) from exc

        click.echo(render_table(report), err=True)
        if emit_json:
            click.echo(json.dumps(report.to_dict(), indent=2, sort_keys=True))
        if any(r.severity is Severity.FAIL for r in report.results):
            raise SystemExit(1)
        return

    # --llm-probe is the online counterpart: it parses the config and
    # spends real tokens hitting every configured model endpoint. We
    # load the config first so a parse failure is an exit-2 "internal
    # error" (mirrors the full-doctor path); the probe itself reports
    # per-model FAIL rows with exit 1 when a model is misconfigured.
    if llm_probe:
        try:
            loaded = MaintainerConfig.from_yaml(config)
        except FileNotFoundError as exc:
            click.echo(f"doctor: config file not found: {exc}", err=True)
            raise SystemExit(2) from exc
        except Exception as exc:
            click.echo(f"doctor: failed to load config: {exc}", err=True)
            raise SystemExit(2) from exc
        try:
            report = run_llm_probe_sync(loaded)
        except Exception as exc:  # pragma: no cover - surfaced to CLI user
            click.echo(f"doctor: internal error during llm-probe: {exc}", err=True)
            click.echo(traceback.format_exc(), err=True)
            raise SystemExit(2) from exc
        click.echo(render_table(report), err=True)
        if emit_json:
            click.echo(json.dumps(report.to_dict(), indent=2, sort_keys=True))
        if any(r.severity is Severity.FAIL for r in report.results):
            raise SystemExit(1)
        return

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
    """Parse ``--since`` as ``<N>[smhdw]`` or an ISO-8601 timestamp.

    Kept module-local (not a Click type) because the duration grammar is
    tiny and reusing click's DateTime type would lose the ``7d`` case.
    Shared by the eval harness (``caretaker eval run``) and the
    attribution backfill (``caretaker backfill-attribution``).
    """
    import re
    from datetime import UTC, datetime, timedelta

    stripped = value.strip()
    match = re.fullmatch(r"(\d+)([smhdw])", stripped)
    if match:
        amount = int(match.group(1))
        unit = match.group(2)
        delta = {
            "s": timedelta(seconds=amount),
            "m": timedelta(minutes=amount),
            "h": timedelta(hours=amount),
            "d": timedelta(days=amount),
            "w": timedelta(weeks=amount),
        }[unit]
        return datetime.now(UTC) - delta
    try:
        parsed = datetime.fromisoformat(stripped)
    except ValueError as exc:
        raise click.BadParameter(
            f"--since must be a duration like '24h' / '4w' or an ISO-8601 timestamp; got {value!r}"
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


# ── memory ────────────────────────────────────────────────────────────


@main.group("memory")
def memory_group() -> None:
    """Memory-store utilities — backfill embeddings, inspect store."""


@memory_group.command("backfill-embeddings")
@click.option(
    "--since",
    default="30d",
    show_default=True,
    help=(
        "Time window: '7d', '30d', '72h'. Only nodes whose observed_at is "
        "within this window are backfilled."
    ),
)
@click.option(
    "--config",
    required=False,
    type=click.Path(exists=True),
    help="Path to config.yml (used to read memory_store + embedding settings).",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="List candidate nodes without writing embeddings.",
)
@click.option(
    "--labels",
    default="Incident,AgentCoreMemory",
    show_default=True,
    help="Comma-separated list of node labels to backfill.",
)
def backfill_embeddings(
    since: str,
    config: str | None,
    dry_run: bool,
    labels: str,
) -> None:
    """Populate ``summary_embedding`` on existing memory nodes.

    Wave A3 companion to the fix-ladder: walks ``:Incident`` and
    ``:AgentCoreMemory`` nodes that have a summary but no
    ``summary_embedding`` and writes the vector so Wave B3's
    Neo4j-native vector-index retriever has a corpus to work on.

    Exits ``0`` on success (including zero-candidate runs); ``2`` on
    configuration / setup errors.
    """
    from caretaker.memory.backfill import run_backfill_sync

    result = run_backfill_sync(
        since=since,
        config_path=config,
        dry_run=dry_run,
        labels=[label.strip() for label in labels.split(",") if label.strip()],
    )
    click.echo(json.dumps(result, indent=2, sort_keys=True))
    if result.get("errors"):
        raise SystemExit(1)


# ── attribution backfill ──────────────────────────────────────────────────

# Local shims so ``backfill_attribution`` stays readable without pulling
# every model symbol into the top of the file.
_PR_MERGED = "merged"
_PR_ESCALATED = "escalated"
_PR_CLOSED = "closed"
_ISSUE_STALE = "stale"
_ISSUE_CLOSED = "closed"


def _row_active_since(row: object, cutoff: datetime) -> bool:
    """Return True if the tracked row has any activity on or after ``cutoff``.

    Uses ``last_checked`` first (updated on every cycle), then
    ``merged_at`` / ``first_seen_at`` as fallbacks. Rows with no
    timestamps at all are included — they're the most-suspect candidates
    for backfill, and filtering them out would leave the weekly
    dashboard permanently blind on them.
    """
    from datetime import UTC

    stamp = (
        getattr(row, "last_checked", None)
        or getattr(row, "merged_at", None)
        or getattr(row, "first_seen_at", None)
    )
    if stamp is None:
        return True
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    if cutoff.tzinfo is None:
        cutoff = cutoff.replace(tzinfo=UTC)
    return bool(stamp >= cutoff)


@main.command("backfill-attribution")
@click.option(
    "--config",
    default=DEFAULT_DOCTOR_CONFIG,
    show_default=True,
    type=click.Path(),
    help="Path to config.yml. Defaults to .github/maintainer/config.yml.",
)
@click.option(
    "--since",
    default="30d",
    show_default=True,
    help=(
        "Look back window. Accepts 'Nd' / 'Nw' / 'Nh' or an ISO-8601 datetime. "
        "Rows older than this are skipped."
    ),
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Print what would change without mutating persisted state.",
)
def backfill_attribution(config: str, since: str, dry_run: bool) -> None:
    """Backfill attribution telemetry on existing tracked state.

    Walks every PR / issue in the orchestrator state store and reconciles
    the attribution fields (``caretaker_touched`` / ``caretaker_merged``
    / ``caretaker_closed``) against what can be inferred from the
    existing history. Best-effort: any field that can't be reconstructed
    is left at its default, which renders as "unknown" in the dashboard.

    This is a one-shot. Running it more than once on the same state is
    a no-op for rows that are already coherent.
    """
    import asyncio as _asyncio
    import json as _json
    from datetime import UTC, datetime, timedelta

    from caretaker.orchestrator import Orchestrator
    from caretaker.state.intervention_detector import backfill_missing_fields

    _configure_logging(None, debug=False)

    try:
        loaded = MaintainerConfig.from_yaml(config)
    except FileNotFoundError as exc:
        click.echo(f"backfill-attribution: config file not found: {exc}", err=True)
        raise SystemExit(2) from exc

    cutoff = _parse_since(since)
    del loaded
    orch = Orchestrator.from_config_path(config)

    async def _run() -> tuple[int, int, int]:
        await orch._state_tracker.load()
        state = orch._state_tracker.state
        filtered_prs = {
            n: pr for n, pr in state.tracked_prs.items() if _row_active_since(pr, cutoff)
        }
        filtered_issues = {
            n: issue
            for n, issue in state.tracked_issues.items()
            if _row_active_since(issue, cutoff)
        }
        pr_inferred = 0
        for pr in filtered_prs.values():
            if pr.state == _PR_MERGED and not pr.caretaker_merged:
                pr.caretaker_merged = True
                pr.caretaker_touched = True
                pr_inferred += 1
            elif pr.state == _PR_ESCALATED and not pr.caretaker_touched:
                pr.caretaker_touched = True
                pr_inferred += 1
        issue_inferred = 0
        for issue in filtered_issues.values():
            if issue.state in (_ISSUE_STALE, _ISSUE_CLOSED) and not issue.caretaker_touched:
                issue.caretaker_touched = True
                if issue.state == _ISSUE_STALE:
                    issue.caretaker_closed = True
                issue_inferred += 1
        reconciled = backfill_missing_fields(state.tracked_prs, state.tracked_issues)
        if dry_run:
            click.echo(
                _json.dumps(
                    {
                        "dry_run": True,
                        "since": cutoff.isoformat(),
                        "prs_in_window": len(filtered_prs),
                        "issues_in_window": len(filtered_issues),
                        "prs_inferred": pr_inferred,
                        "issues_inferred": issue_inferred,
                        "invariant_reconciliations": reconciled,
                    },
                    indent=2,
                )
            )
            return pr_inferred, issue_inferred, reconciled
        if pr_inferred or issue_inferred or reconciled:
            await orch._state_tracker.save()
        return pr_inferred, issue_inferred, reconciled

    try:
        pr_inferred, issue_inferred, reconciled = _asyncio.run(_run())
    except Exception as exc:  # pragma: no cover - surfaced to CLI user
        click.echo(f"backfill-attribution: internal error: {exc}", err=True)
        raise SystemExit(2) from exc

    click.echo(
        f"Backfill complete: PRs updated={pr_inferred} Issues updated={issue_inferred} "
        f"Invariant fixes={reconciled}"
    )
    _ = datetime.now(UTC) - timedelta(days=1)


@main.command("init-workflow")
@click.option(
    "--output",
    default=".github/workflows/maintainer.yml",
    show_default=True,
    help="Destination path for the generated workflow file.",
)
@click.option(
    "--llm",
    "llm_provider",
    type=click.Choice(["azure-ai", "openai", "anthropic", "all"], case_sensitive=False),
    default="all",
    show_default=True,
    help="LLM provider(s) to include secret blocks for.",
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Overwrite an existing workflow file without prompting.",
)
def init_workflow(output: str, llm_provider: str, force: bool) -> None:
    """Generate a ready-to-use caretaker maintainer workflow.

    Writes the canonical workflow template to OUTPUT (default:
    .github/workflows/maintainer.yml). The template includes the correct
    permissions block, all required pull_request trigger types, and secret
    blocks for the chosen LLM provider(s).

    \b
    Examples
    --------
    # Minimal — Azure AI Foundry only
    caretaker init-workflow --llm azure-ai

    # All providers (default), custom path
    caretaker init-workflow --output .github/workflows/caretaker.yml

    \b
    After generation
    ----------------
    1. Add the secrets listed in the workflow to your repo / org secrets.
    2. Copy docs/examples/config.yml to .github/maintainer/config.yml and
       edit to taste (or run ``caretaker validate-config`` afterwards).
    3. Commit and push — the first scheduled run fires within 15 minutes.
    """
    import importlib.resources as _ir

    # Locate the bundled template
    try:
        # Python 3.9+ path
        ref = _ir.files("caretaker") / "../../templates/maintainer.yml"
        template_text = ref.read_text(encoding="utf-8")
    except Exception as _load_err:  # noqa: BLE001
        # Fallback: look relative to this file
        template_path = Path(__file__).parent.parent.parent / "templates" / "maintainer.yml"
        if not template_path.is_file():
            click.echo(
                "caretaker init-workflow: bundled template not found. "
                "Please file a bug at https://github.com/ianlintner/caretaker",
                err=True,
            )
            raise SystemExit(1) from _load_err
        template_text = template_path.read_text(encoding="utf-8")

    # Filter LLM secret blocks based on --llm flag
    _provider_vars = {
        "azure-ai": ["AZURE_AI_API_KEY", "AZURE_AI_API_BASE", "AZURE_AI_API_VERSION"],
        "openai": ["OPENAI_API_KEY"],
        "anthropic": ["ANTHROPIC_API_KEY"],
    }
    if llm_provider != "all":
        keep_vars = set(_provider_vars.get(llm_provider, []))
        all_vars = {v for vlist in _provider_vars.values() for v in vlist}
        drop_vars = all_vars - keep_vars
        lines: list[str] = []
        for line in template_text.splitlines(keepends=True):
            stripped = line.strip()
            if any(dv in stripped for dv in drop_vars):
                continue  # drop secret line and surrounding comment if any
            lines.append(line)
        template_text = "".join(lines)

    dest = Path(output)
    if dest.exists() and not force:
        click.confirm(
            f"'{dest}' already exists. Overwrite?",
            abort=True,
        )

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(template_text, encoding="utf-8")

    click.echo(f"Wrote workflow to {dest}")
    click.echo("")
    click.echo("Next steps:")
    click.echo("  1. Add secrets to your repo: Settings → Secrets and variables → Actions")
    if llm_provider in ("azure-ai", "all"):
        click.echo("     AZURE_AI_API_KEY, AZURE_AI_API_BASE, AZURE_AI_API_VERSION")
    if llm_provider in ("openai", "all"):
        click.echo("     OPENAI_API_KEY")
    if llm_provider in ("anthropic", "all"):
        click.echo("     ANTHROPIC_API_KEY")
    click.echo("  2. Create .github/maintainer/config.yml (copy from docs/examples/config.yml)")
    click.echo("  3. Commit and push — first scheduled run fires within 15 min.")


@main.group("fleet")
def fleet_group() -> None:  # pragma: no cover
    """Commands for auditing the caretaker consumer fleet."""


@fleet_group.command("lag")
@click.option(
    "--fleet",
    "fleet_file",
    default="docs/fleet.yml",
    show_default=True,
    help="Path to the fleet membership YAML (docs/fleet.yml).",
)
@click.option(
    "--releases",
    "releases_file",
    default="releases.json",
    show_default=True,
    help="Path to releases.json that records the latest caretaker version.",
)
@click.option(
    "--output",
    default=None,
    help="Write the JSON report to this file in addition to stdout.",
)
@click.option(
    "--fail-on-violations",
    is_flag=True,
    default=False,
    help="Exit with code 1 when any repo violates the lag or stuck-PR thresholds.",
)
@click.option(
    "--file-issues",
    is_flag=True,
    default=False,
    help="Open a GitHub Issue in each laggard repo (requires GH_TOKEN env var).",
)
def fleet_lag(  # noqa: C901  (complexity: intentionally inline for CLI clarity)
    fleet_file: str,
    releases_file: str,
    output: str | None,
    fail_on_violations: bool,
    file_issues: bool,
) -> None:
    """Audit consumer repos for version lag and stuck upgrade PRs.

    Reads docs/fleet.yml for the list of repos and releases.json for the
    latest caretaker version, then queries the GitHub API to check each
    repo's pinned version and open upgrade PRs.

    Exit codes:
      0 — all repos are within the allowed lag window
      1 — one or more repos violate lag/stuck-PR thresholds (with --fail-on-violations)
      2 — internal error (missing files, bad auth, etc.)

    \b
    Examples
    --------
    # Print a summary table to stdout
    caretaker fleet lag

    # Write JSON report and fail CI if any violations found
    caretaker fleet lag --output fleet-lag-report.json --fail-on-violations
    """
    import datetime
    import json as _json
    import re
    import urllib.request as _req
    from pathlib import Path as _Path
    from typing import Any

    import yaml  # already a transitive dep via pyyaml

    # ------------------------------------------------------------------
    # Load fleet config
    # ------------------------------------------------------------------
    fleet_path = _Path(fleet_file)
    if not fleet_path.is_file():
        click.echo(f"fleet lag: fleet file not found: {fleet_file}", err=True)
        raise SystemExit(2)

    releases_path = _Path(releases_file)
    if not releases_path.is_file():
        click.echo(f"fleet lag: releases file not found: {releases_file}", err=True)
        raise SystemExit(2)

    fleet_cfg = yaml.safe_load(fleet_path.read_text())
    thresholds = fleet_cfg.get("thresholds", {})
    max_lag_minor: int = thresholds.get("max_version_lag_minor", 2)
    max_stuck_days: int = thresholds.get("max_stuck_pr_days", 7)

    releases_data = _json.loads(releases_path.read_text())
    latest_version: str = releases_data["releases"][0]["version"]

    def _parse_version(v: str) -> tuple[int, int, int]:
        """Parse 'X.Y.Z' → (X, Y, Z); fall back to (0, 0, 0) on error."""
        m = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", v.lstrip("v"))
        if not m:
            return (0, 0, 0)
        return (int(m.group(1)), int(m.group(2)), int(m.group(3)))

    latest_tuple = _parse_version(latest_version)

    gh_token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")

    def _gh_get(path: str) -> dict[str, Any] | list[Any] | None:
        """Call the GitHub REST API; return parsed JSON or None on error."""
        url = f"https://api.github.com{path}"
        headers: dict[str, str] = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if gh_token:
            headers["Authorization"] = f"Bearer {gh_token}"
        try:
            req = _req.Request(url, headers=headers)
            with _req.urlopen(req, timeout=15) as resp:
                result: dict[str, Any] | list[Any] = _json.loads(resp.read())
                return result
        except Exception as exc:  # pragma: no cover
            click.echo(f"  [warn] GitHub API error for {path}: {exc}", err=True)
            return None

    # ------------------------------------------------------------------
    # Audit each repo
    # ------------------------------------------------------------------
    now = datetime.datetime.now(datetime.UTC)
    results: list[dict[str, Any]] = []
    violations: list[str] = []

    for entry in fleet_cfg.get("fleet", []):
        repo: str = entry["repo"]
        role: str = entry.get("role", "production")
        notes: str = entry.get("notes", "")

        # 1. Fetch pinned version
        pinned_version = "unknown"
        version_resp = _gh_get(f"/repos/{repo}/contents/.github/maintainer/.version")
        if isinstance(version_resp, dict) and "content" in version_resp:
            import base64

            raw = base64.b64decode(version_resp["content"]).decode().strip()
            pinned_version = raw

        pinned_tuple = _parse_version(pinned_version)
        same_major = latest_tuple[0] == pinned_tuple[0]
        minor_lag = (latest_tuple[1] - pinned_tuple[1]) if same_major else 99

        # 2. Fetch open upgrade PRs
        prs_resp = _gh_get(f"/repos/{repo}/pulls?state=open&per_page=50")
        open_upgrade_prs: list[dict[str, Any]] = []
        if isinstance(prs_resp, list):
            for pr in prs_resp:
                title: str = pr.get("title", "")
                if "upgrade" in title.lower() and "caretaker" in title.lower():
                    created_at_str: str = pr.get("created_at", "")
                    try:
                        created_at = datetime.datetime.fromisoformat(
                            created_at_str.rstrip("Z") + "+00:00"
                        )
                        age_days = (now - created_at).days
                    except ValueError:
                        age_days = 0
                    open_upgrade_prs.append(
                        {
                            "number": pr["number"],
                            "title": title,
                            "draft": pr.get("draft", False),
                            "age_days": age_days,
                        }
                    )

        stuck_prs = [p for p in open_upgrade_prs if p["age_days"] > max_stuck_days]

        # 3. Determine violations
        repo_violations: list[str] = []
        if pinned_version != "unknown" and minor_lag >= max_lag_minor:
            repo_violations.append(
                f"version lag {minor_lag} minor versions "
                f"(pinned={pinned_version} latest={latest_version})"
            )
        for stuck in stuck_prs:
            repo_violations.append(
                f"upgrade PR #{stuck['number']} stuck for {stuck['age_days']} days"
            )

        status = "OK" if not repo_violations else "VIOLATION"
        if repo_violations:
            violations.append(repo)

        result_entry = {
            "repo": repo,
            "role": role,
            "pinned_version": pinned_version,
            "latest_version": latest_version,
            "minor_lag": minor_lag,
            "open_upgrade_prs": open_upgrade_prs,
            "stuck_prs_count": len(stuck_prs),
            "violations": repo_violations,
            "status": status,
        }
        if notes:
            result_entry["notes"] = notes
        results.append(result_entry)

    # ------------------------------------------------------------------
    # Optionally file GitHub Issues for violations
    # ------------------------------------------------------------------
    if file_issues and violations and gh_token:
        for entry_result in results:
            if entry_result["status"] != "VIOLATION":
                continue
            repo = entry_result["repo"]
            body_lines = [
                "## caretaker Fleet Lag Alert",
                "",
                "This repo is lagging behind the latest caretaker release.",
                "",
                "| Field | Value |",
                "|---|---|",
                f"| Pinned version | `{entry_result['pinned_version']}` |",
                f"| Latest version | `{entry_result['latest_version']}` |",
                f"| Minor version lag | {entry_result['minor_lag']} |",
                f"| Stuck upgrade PRs | {entry_result['stuck_prs_count']} |",
                "",
                "**Violations:**",
            ]
            for v in entry_result["violations"]:
                body_lines.append(f"- {v}")
            body_lines += [
                "",
                "_Auto-filed by `caretaker fleet lag --file-issues`._",
            ]
            payload = _json.dumps(
                {
                    "title": (
                        f"[caretaker] Fleet lag: pinned to"
                        f" {entry_result['pinned_version']},"
                        f" latest is {latest_version}"
                    ),
                    "body": "\n".join(body_lines),
                    "labels": ["caretaker:fleet-lag"],
                }
            ).encode()
            url = f"https://api.github.com/repos/{repo}/issues"
            headers = {
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "Authorization": f"Bearer {gh_token}",
                "Content-Type": "application/json",
            }
            try:
                req = _req.Request(url, data=payload, headers=headers, method="POST")
                with _req.urlopen(req, timeout=15) as resp:
                    issue_data = _json.loads(resp.read())
                    click.echo(f"  [info] Filed issue #{issue_data['number']} in {repo}")
            except Exception as exc:  # pragma: no cover
                click.echo(f"  [warn] Failed to file issue in {repo}: {exc}", err=True)

    # ------------------------------------------------------------------
    # Render output
    # ------------------------------------------------------------------
    report: dict[str, Any] = {
        "generated_at": now.isoformat(),
        "latest_caretaker_version": latest_version,
        "thresholds": {
            "max_version_lag_minor": max_lag_minor,
            "max_stuck_pr_days": max_stuck_days,
        },
        "summary": {
            "total_repos": len(results),
            "ok": sum(1 for r in results if r["status"] == "OK"),
            "violations": len(violations),
        },
        "repos": results,
    }

    report_json = _json.dumps(report, indent=2)

    if output:
        _Path(output).write_text(report_json)
        click.echo(f"Report written to {output}")

    # Print a compact table to stdout
    click.echo(f"\nFleet lag report — latest caretaker: v{latest_version}\n" + "-" * 72)
    col = "{:<35} {:<10} {:<10} {:<10} {}"
    click.echo(col.format("REPO", "PINNED", "LAG", "STUCK_PRS", "STATUS"))
    click.echo("-" * 72)
    for r in results:
        lag_str = f"+{r['minor_lag']}" if isinstance(r["minor_lag"], int) else "?"
        click.echo(
            col.format(
                r["repo"].split("/")[1][:34],
                r["pinned_version"],
                lag_str,
                str(r["stuck_prs_count"]),
                r["status"],
            )
        )
    click.echo("-" * 72)
    click.echo(
        f"Summary: {report['summary']['ok']} OK / "
        f"{report['summary']['violations']} violation(s) / "
        f"{report['summary']['total_repos']} total\n"
    )

    if violations and fail_on_violations:
        raise SystemExit(1)


@fleet_group.command("status")
@click.option(
    "--config",
    "config_path",
    default=".github/maintainer/config.yml",
    show_default=True,
    help="Path to the maintainer config that contains the fleet_registry block.",
)
def fleet_status(config_path: str) -> None:
    """Print the local fleet_registry config and resolved emitter target.

    Useful when verifying that a child repo is opted in: shows whether
    fleet_registry.enabled is true, the configured endpoint, and whether the
    HMAC secret env var is populated. Performs no network I/O.
    """
    cfg_path = Path(config_path)
    if not cfg_path.is_file():
        click.echo(f"❌ Config not found: {cfg_path}", err=True)
        raise SystemExit(2)
    try:
        cfg = MaintainerConfig.from_yaml(cfg_path)
    except Exception as exc:  # pragma: no cover - surfaces config errors
        click.echo(f"❌ Failed to load config {cfg_path}: {exc}", err=True)
        raise SystemExit(2) from exc

    registry = cfg.fleet_registry
    repo_slug = os.environ.get("GITHUB_REPOSITORY") or "<unset>"
    secret_env = registry.secret_env or "CARETAKER_FLEET_SECRET"
    secret_present = bool(os.environ.get(secret_env))

    click.echo("Caretaker fleet — local status")
    click.echo("-" * 72)
    click.echo(f"Config file              : {cfg_path}")
    click.echo(f"Repo (GITHUB_REPOSITORY) : {repo_slug}")
    click.echo(f"fleet_registry.enabled   : {registry.enabled}")
    click.echo(f"fleet_registry.endpoint  : {registry.endpoint or '<unset>'}")
    click.echo(f"fleet_registry.secret_env: {secret_env}")
    secret_status = "yes" if secret_present else "no (unsigned heartbeats)"
    click.echo(f"  secret env populated   : {secret_status}")
    click.echo(f"include_full_summary     : {registry.include_full_summary}")
    click.echo(f"timeout_seconds          : {registry.timeout_seconds}")
    if registry.oauth2 and registry.oauth2.enabled:
        click.echo("oauth2                   : enabled (will fetch bearer token)")
    else:
        click.echo("oauth2                   : disabled")

    if not registry.enabled:
        click.echo("\n⚠ fleet_registry is disabled — heartbeats will NOT be sent.")
        click.echo("  Set fleet_registry.enabled: true in config.yml to opt in.")
    elif not registry.endpoint:
        click.echo("\n❌ fleet_registry.enabled is true but endpoint is missing.", err=True)
        raise SystemExit(1)
    else:
        click.echo("\n✓ Configuration looks ready. Run `caretaker fleet register-self` to verify.")


@fleet_group.command("register-self")
@click.option(
    "--config",
    "config_path",
    default=".github/maintainer/config.yml",
    show_default=True,
    help="Path to the maintainer config that contains the fleet_registry block.",
)
@click.option(
    "--repo",
    "repo_override",
    default=None,
    help="Repository slug (owner/name). Defaults to $GITHUB_REPOSITORY.",
)
def fleet_register_self(config_path: str, repo_override: str | None) -> None:
    """Send a single verification heartbeat to the fleet registry.

    Builds a minimal heartbeat payload from the local config and POSTs it to
    the configured endpoint. Honours the same HMAC + OAuth2 wiring as the
    in-process emitter. Use this once after opting a repo in to confirm the
    backend is receiving signed payloads end-to-end.
    """
    cfg_path = Path(config_path)
    if not cfg_path.is_file():
        click.echo(f"❌ Config not found: {cfg_path}", err=True)
        raise SystemExit(2)
    try:
        cfg = MaintainerConfig.from_yaml(cfg_path)
    except Exception as exc:  # pragma: no cover
        click.echo(f"❌ Failed to load config {cfg_path}: {exc}", err=True)
        raise SystemExit(2) from exc

    registry = cfg.fleet_registry
    if not registry.enabled:
        click.echo("❌ fleet_registry.enabled must be true. Edit your config.yml first.", err=True)
        raise SystemExit(2)
    if not registry.endpoint:
        click.echo(
            "❌ fleet_registry.endpoint is missing. Set it to the admin /api/fleet/heartbeat URL.",
            err=True,
        )
        raise SystemExit(2)

    repo_slug = repo_override or os.environ.get("GITHUB_REPOSITORY")
    if not repo_slug:
        click.echo(
            "❌ Cannot determine repo slug. Pass --repo owner/name or set GITHUB_REPOSITORY.",
            err=True,
        )
        raise SystemExit(2)

    # Lazy imports to avoid pulling httpx/Pydantic into the cold-start path of
    # unrelated CLI commands.

    from caretaker.fleet.emitter import build_heartbeat
    from caretaker.state.models import RunSummary

    # Provide GITHUB_REPOSITORY for build_heartbeat's slug fallback when --repo
    # was passed explicitly but the env var is unset.
    if repo_override and not os.environ.get("GITHUB_REPOSITORY"):
        os.environ["GITHUB_REPOSITORY"] = repo_override

    summary = RunSummary(mode="register-self")
    try:
        heartbeat = build_heartbeat(
            cfg,
            summary,
            repo=repo_slug,
            include_full_summary=False,
            state=None,
        )
    except Exception as exc:  # pragma: no cover - surfaces config errors
        click.echo(f"❌ Failed to build heartbeat payload: {exc}", err=True)
        raise SystemExit(1) from exc

    click.echo(f"→ Posting verification heartbeat for {repo_slug} to {registry.endpoint}")
    ok = _post_heartbeat_manual(registry, heartbeat)

    if ok:
        click.echo("✓ Heartbeat accepted by registry.")
    else:
        click.echo("❌ Heartbeat was rejected or the request failed. Check backend logs.", err=True)
        raise SystemExit(1)


def _post_heartbeat_manual(registry, heartbeat) -> bool:
    """POST a built FleetHeartbeat directly using httpx.

    Mirrors the production wire format: JSON body, optional X-Caretaker-Signature
    HMAC-SHA256 header derived from the configured secret env var. Used by the
    register-self CLI so we don't need an event-loop wrapper.
    """
    import hashlib
    import hmac

    import httpx

    body = heartbeat.model_dump_json().encode()
    headers = {"Content-Type": "application/json"}
    secret_env = registry.secret_env or "CARETAKER_FLEET_SECRET"
    secret = os.environ.get(secret_env, "").strip()
    if secret:
        sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        headers["X-Caretaker-Signature"] = f"sha256={sig}"
    else:
        click.echo(f"  (no {secret_env} set — sending unsigned)")
    try:
        with httpx.Client(timeout=max(registry.timeout_seconds, 5.0)) as client:
            resp = client.post(str(registry.endpoint), content=body, headers=headers)
        click.echo(f"  HTTP {resp.status_code}")
        if resp.status_code >= 400:
            click.echo(f"  Response: {resp.text[:400]}", err=True)
            return False
        with contextlib.suppress(ValueError):
            click.echo(f"  Response: {resp.json()}")
    except Exception as exc:  # pragma: no cover - network errors
        click.echo(f"  Request failed: {exc}", err=True)
        return False
    return True


if __name__ == "__main__":
    main()
