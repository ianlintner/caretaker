"""Opt-in fleet-registry heartbeat emitter.

The emitter is intentionally small and fail-open: a misconfigured or
unreachable fleet endpoint must never fail a caretaker run. All errors
are logged at ``WARNING`` and swallowed.

See :class:`caretaker.config.FleetRegistryConfig` for the user-facing
configuration surface.
"""

from __future__ import annotations

import importlib.metadata
import json
import logging
import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import httpx
from pydantic import BaseModel, Field

from caretaker.auth import OAuth2ClientCredentials, OAuth2TokenError, build_client_from_env

if TYPE_CHECKING:
    from caretaker.config import FleetRegistryConfig, MaintainerConfig
    from caretaker.state.models import RunSummary

logger = logging.getLogger(__name__)


_COUNTERS = (
    "prs_monitored",
    "prs_merged",
    "prs_escalated",
    "issues_triaged",
    "issues_assigned",
    "issues_closed",
    "issues_escalated",
    "orphaned_prs",
    "prs_fix_requested",
    "build_failures_detected",
    "self_heal_failures_analyzed",
    "security_findings_found",
    "dependency_prs_reviewed",
    "docs_prs_analyzed",
    "charlie_managed_issues",
    "charlie_managed_prs",
    "stale_issues_warned",
    "escalation_items_found",
    "owned_prs",
    "authority_merges",
)


class FleetOAuthClientCache:
    """Per-owner cache for the fleet heartbeat's OAuth2 client.

    Caching the client across heartbeats is what lets the in-process JWT
    cache inside :class:`OAuth2ClientCredentials` actually deliver value;
    without it, every run would refetch a token. The cache key covers
    every config + env var the client depends on, so a credential
    rotation invalidates automatically.

    One instance is expected per logical owner — :class:`Orchestrator` in
    production, or a test. A :data:`_default_cache` module singleton is
    kept for the legacy free-function call path (``emit_heartbeat`` with
    no ``oauth_cache=`` kwarg), but the singleton is no longer the only
    place the state lives: multiple orchestrators or a multi-tenant admin
    process each get their own instance and never cross-contaminate.
    """

    __slots__ = ("_client", "_key")

    _KeyT = tuple[str, str, str, str, str, float]

    def __init__(self) -> None:
        self._client: OAuth2ClientCredentials | None = None
        self._key: FleetOAuthClientCache._KeyT | None = None

    def get(self, fleet: FleetRegistryConfig) -> OAuth2ClientCredentials | None:
        oauth_cfg = fleet.oauth2
        if not oauth_cfg.enabled:
            return None

        scope = os.environ.get(oauth_cfg.scope_env, "").strip() or oauth_cfg.default_scope
        key: FleetOAuthClientCache._KeyT = (
            os.environ.get(oauth_cfg.client_id_env, ""),
            os.environ.get(oauth_cfg.client_secret_env, ""),
            os.environ.get(oauth_cfg.token_url_env, ""),
            oauth_cfg.scope_env,
            scope,
            oauth_cfg.timeout_seconds,
        )
        if self._key == key and self._client is not None:
            return self._client

        client = build_client_from_env(
            client_id_env=oauth_cfg.client_id_env,
            client_secret_env=oauth_cfg.client_secret_env,
            token_url_env=oauth_cfg.token_url_env,
            scope_env=oauth_cfg.scope_env,
            timeout_seconds=oauth_cfg.timeout_seconds,
        )
        self._client = client
        self._key = key if client is not None else None
        return client

    def invalidate(self) -> None:
        self._client = None
        self._key = None


_default_cache = FleetOAuthClientCache()


async def _oauth_bearer_headers(
    fleet: FleetRegistryConfig,
    *,
    client: httpx.AsyncClient,
    cache: FleetOAuthClientCache,
) -> dict[str, str]:
    """Return ``Authorization: Bearer …`` if OAuth2 is configured, else ``{}``.

    Failures are logged at WARNING and swallowed: the fleet emitter is
    fail-open, so a flaky auth server never breaks the run loop.
    """
    oauth = cache.get(fleet)
    if oauth is None:
        return {}
    try:
        return await oauth.authorization_header(client=client)
    except OAuth2TokenError as exc:
        logger.warning("fleet heartbeat: oauth2 token fetch failed: %s", exc)
        return {}


def _caretaker_version() -> str:
    try:
        return importlib.metadata.version("caretaker-github")
    except importlib.metadata.PackageNotFoundError:
        try:
            return importlib.metadata.version("caretaker")
        except importlib.metadata.PackageNotFoundError:
            return "unknown"


def _enabled_agents(config: MaintainerConfig) -> list[str]:
    """Return the names of agents whose ``enabled`` flag is True."""
    names: list[str] = []
    for attr in (
        "pr_agent",
        "issue_agent",
        "upgrade_agent",
        "devops_agent",
        "self_heal_agent",
        "security_agent",
        "dependency_agent",
        "docs_agent",
        "charlie_agent",
        "stale_agent",
        "review_agent",
        "principal_agent",
        "test_agent",
        "refactor_agent",
        "perf_agent",
        "migration_agent",
    ):
        block = getattr(config, attr, None)
        if block is not None and getattr(block, "enabled", False):
            names.append(attr)
    return names


class AttributionSummary(BaseModel):
    """Per-heartbeat attribution rollup (R&D workstream A2).

    Optional block on :class:`FleetHeartbeat` so the central registry can
    aggregate cross-repo counts without having to scrape each repo's
    orchestrator state directly. Mirrors the shape of the admin
    ``GET /api/admin/attribution/weekly`` response so the backend can
    use a single DTO end-to-end.

    All counts are scoped to the heartbeat's *reporting window* — for
    the full-repo scheduled run that's whatever the orchestrator
    observed in this cycle, for a single-PR event run that's typically
    just the touched PR. The registry is expected to sum these over the
    last N heartbeats per repo to get a weekly rollup, not to trust a
    single heartbeat as canonical. Every field defaults to 0 / None so
    older caretakers that don't emit this block still validate.
    """

    touched: int = 0
    merged: int = 0
    operator_rescued: int = 0
    abandoned: int = 0
    # ``None`` when no caretaker merges landed in the window so the
    # dashboard can render "unknown" instead of a misleading zero.
    avg_time_to_merge_hours: float | None = None


class FleetHeartbeat(BaseModel):
    """Canonical heartbeat payload.

    Backwards compat: the backend MUST tolerate unknown fields. Keep
    the counters set flat; nest anything richer under ``summary`` so
    older backends that pin the counters schema still parse.
    """

    schema_version: int = 1
    repo: str
    caretaker_version: str
    run_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    mode: str = "full"
    enabled_agents: list[str] = Field(default_factory=list)
    goal_health: float | None = None
    error_count: int = 0
    counters: dict[str, int] = Field(default_factory=dict)
    summary: dict[str, Any] | None = None
    # Optional attribution rollup. ``None`` keeps the payload byte-
    # identical to pre-A2 for older emitters; the current emitter
    # always populates this block even when everything is zero, so the
    # backend can distinguish "no data" from "zero activity."
    attribution: AttributionSummary | None = None


def _build_attribution_summary(state: Any | None) -> AttributionSummary | None:
    """Roll tracked-PR attribution into an :class:`AttributionSummary`.

    Returns ``None`` when no state is supplied — the heartbeat field is
    optional and older callers that never passed a state should keep
    their payload byte-identical. When state is supplied we always
    return a summary, even if every counter is zero, so the backend can
    tell "zero activity this window" from "older caretaker that doesn't
    report attribution yet."
    """
    if state is None:
        return None
    prs = list(getattr(state, "tracked_prs", {}).values())
    touched = sum(1 for pr in prs if getattr(pr, "caretaker_touched", False))
    merged = sum(1 for pr in prs if getattr(pr, "caretaker_merged", False))
    rescued = sum(1 for pr in prs if getattr(pr, "operator_intervened", False))
    abandoned = sum(
        1
        for pr in prs
        if getattr(getattr(pr, "state", None), "value", None) == "escalated"
        and not getattr(pr, "operator_intervened", False)
    )
    merged_prs = [
        pr
        for pr in prs
        if getattr(pr, "caretaker_merged", False)
        and getattr(pr, "merged_at", None) is not None
        and getattr(pr, "first_seen_at", None) is not None
    ]
    ttm: float | None = None
    if merged_prs:
        total = 0.0
        for pr in merged_prs:
            first = pr.first_seen_at
            merged_at = pr.merged_at
            if first.tzinfo is None:
                first = first.replace(tzinfo=UTC)
            if merged_at.tzinfo is None:
                merged_at = merged_at.replace(tzinfo=UTC)
            total += (merged_at - first).total_seconds() / 3600.0
        ttm = round(total / len(merged_prs), 2)
    return AttributionSummary(
        touched=touched,
        merged=merged,
        operator_rescued=rescued,
        abandoned=abandoned,
        avg_time_to_merge_hours=ttm,
    )


def build_heartbeat(
    config: MaintainerConfig,
    summary: RunSummary,
    *,
    repo: str | None = None,
    include_full_summary: bool | None = None,
    state: Any | None = None,
) -> FleetHeartbeat:
    """Assemble the heartbeat payload from a finished run.

    ``state`` is the orchestrator's ``OrchestratorState`` snapshot; when
    supplied the heartbeat carries an :class:`AttributionSummary` block
    so the fleet registry can aggregate cross-repo attribution counts
    without scraping each consumer. Kept optional for backward
    compatibility and for legacy test fixtures that never constructed
    state.
    """
    slug = repo or os.environ.get("GITHUB_REPOSITORY") or "unknown/unknown"
    counters = {k: int(getattr(summary, k, 0) or 0) for k in _COUNTERS}
    want_full = (
        include_full_summary
        if include_full_summary is not None
        else config.fleet_registry.include_full_summary
    )
    full = summary.model_dump(mode="json") if want_full else None
    return FleetHeartbeat(
        repo=slug,
        caretaker_version=_caretaker_version(),
        run_at=summary.run_at if summary.run_at.tzinfo else summary.run_at.replace(tzinfo=UTC),
        mode=summary.mode,
        enabled_agents=_enabled_agents(config),
        goal_health=summary.goal_health,
        error_count=len(summary.errors or []),
        counters=counters,
        summary=full,
        attribution=_build_attribution_summary(state),
    )


async def emit_heartbeat(
    config: MaintainerConfig,
    summary: RunSummary,
    *,
    client: httpx.AsyncClient | None = None,
    oauth_cache: FleetOAuthClientCache | None = None,
    state: Any | None = None,
) -> bool:
    """POST a heartbeat to the configured fleet endpoint.

    Returns ``True`` on a 2xx response, ``False`` otherwise. Never raises:
    network or configuration problems are logged at ``WARNING`` and
    swallowed so a failure to register never fails the orchestrator run.

    ``oauth_cache`` lets the caller own the OAuth2 client cache (needed
    when multiple configs share a process, e.g. the admin backend or
    tests). When omitted, the module-level :data:`_default_cache` is used
    for the single-owner default case.
    """
    fleet: FleetRegistryConfig = config.fleet_registry
    if not fleet.enabled:
        return False
    endpoint = fleet.endpoint
    if not endpoint:
        logger.debug("fleet_registry.enabled but endpoint is empty; skipping")
        return False

    try:
        heartbeat = build_heartbeat(config, summary, state=state)
    except Exception as exc:  # defensive: never break the run loop
        logger.warning("fleet heartbeat: failed to build payload: %s", exc)
        return False

    body = heartbeat.model_dump_json().encode("utf-8")
    headers = {"Content-Type": "application/json"}

    cache = oauth_cache if oauth_cache is not None else _default_cache
    owns_client = client is None
    try:
        if owns_client:
            client = httpx.AsyncClient(timeout=fleet.timeout_seconds)
        assert client is not None  # narrow for type-checkers
        try:
            oauth_headers = await _oauth_bearer_headers(fleet, client=client, cache=cache)
            if not oauth_headers:
                logger.warning(
                    "fleet heartbeat: no OAuth2 bearer token available; "
                    "backend will reject this heartbeat. Verify "
                    "OAUTH2_CLIENT_ID, OAUTH2_CLIENT_SECRET, and "
                    "OAUTH2_TOKEN_URL are set in the runner environment."
                )
            headers.update(oauth_headers)
            resp = await client.post(endpoint, content=body, headers=headers)
        finally:
            if owns_client:
                await client.aclose()
        if 200 <= resp.status_code < 300:
            logger.info(
                "fleet heartbeat: registered %s with %s (status %d)",
                heartbeat.repo,
                endpoint,
                resp.status_code,
            )
            return True
        logger.warning(
            "fleet heartbeat: non-2xx response from %s (status %d, body=%r)",
            endpoint,
            resp.status_code,
            resp.text[:200],
        )
        return False
    except httpx.HTTPError as exc:
        logger.warning("fleet heartbeat: transport error to %s: %s", endpoint, exc)
        return False
    except Exception as exc:  # fail-open: protect the run loop
        logger.warning("fleet heartbeat: unexpected error: %s", exc)
        return False


def heartbeat_as_dict(heartbeat: FleetHeartbeat) -> dict[str, Any]:
    """Small helper for callers that want the JSON-safe dict."""
    result: dict[str, Any] = json.loads(heartbeat.model_dump_json())
    return result
