"""Opt-in fleet-registry heartbeat emitter.

The emitter is intentionally small and fail-open: a misconfigured or
unreachable fleet endpoint must never fail a caretaker run. All errors
are logged at ``WARNING`` and swallowed.

See :class:`caretaker.config.FleetRegistryConfig` for the user-facing
configuration surface.
"""

from __future__ import annotations

import hashlib
import hmac
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


_oauth_client: OAuth2ClientCredentials | None = None
_oauth_client_key: tuple[str, str, str, str, str, float] | None = None


def _get_oauth_client(fleet: FleetRegistryConfig) -> OAuth2ClientCredentials | None:
    """Return a cached OAuth2 client for the given config, rebuilding on change.

    Caching the client instance across heartbeat calls is what lets the
    in-memory JWT cache actually deliver value: without it, each run
    would refetch a token. The cache key covers every config + env var
    the client depends on, so a rotation invalidates correctly.
    """
    global _oauth_client, _oauth_client_key

    oauth_cfg = fleet.oauth2
    if not oauth_cfg.enabled:
        return None

    scope = os.environ.get(oauth_cfg.scope_env, "").strip() or oauth_cfg.default_scope
    key = (
        os.environ.get(oauth_cfg.client_id_env, ""),
        os.environ.get(oauth_cfg.client_secret_env, ""),
        os.environ.get(oauth_cfg.token_url_env, ""),
        oauth_cfg.scope_env,
        scope,
        oauth_cfg.timeout_seconds,
    )
    if _oauth_client_key == key and _oauth_client is not None:
        return _oauth_client

    client = build_client_from_env(
        client_id_env=oauth_cfg.client_id_env,
        client_secret_env=oauth_cfg.client_secret_env,
        token_url_env=oauth_cfg.token_url_env,
        scope_env=oauth_cfg.scope_env,
        timeout_seconds=oauth_cfg.timeout_seconds,
    )
    _oauth_client = client
    _oauth_client_key = key if client is not None else None
    return client


async def _oauth_bearer_headers(
    fleet: FleetRegistryConfig, *, client: httpx.AsyncClient
) -> dict[str, str]:
    """Return ``Authorization: Bearer …`` if OAuth2 is configured, else ``{}``.

    Failures are logged at WARNING and swallowed: the fleet emitter is
    fail-open, so a flaky auth server never breaks the run loop.
    """
    oauth = _get_oauth_client(fleet)
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


def build_heartbeat(
    config: MaintainerConfig,
    summary: RunSummary,
    *,
    repo: str | None = None,
    include_full_summary: bool | None = None,
) -> FleetHeartbeat:
    """Assemble the heartbeat payload from a finished run."""
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
    )


def sign_payload(body: bytes, secret: str) -> str:
    """Compute the hex HMAC-SHA256 signature forwarded in the header."""
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


async def emit_heartbeat(
    config: MaintainerConfig,
    summary: RunSummary,
    *,
    client: httpx.AsyncClient | None = None,
) -> bool:
    """POST a heartbeat to the configured fleet endpoint.

    Returns ``True`` on a 2xx response, ``False`` otherwise. Never raises:
    network or configuration problems are logged at ``WARNING`` and
    swallowed so a failure to register never fails the orchestrator run.
    """
    fleet: FleetRegistryConfig = config.fleet_registry
    if not fleet.enabled:
        return False
    endpoint = fleet.endpoint
    if not endpoint:
        logger.debug("fleet_registry.enabled but endpoint is empty; skipping")
        return False

    try:
        heartbeat = build_heartbeat(config, summary)
    except Exception as exc:  # defensive: never break the run loop
        logger.warning("fleet heartbeat: failed to build payload: %s", exc)
        return False

    body = heartbeat.model_dump_json().encode("utf-8")
    headers = {"Content-Type": "application/json"}
    secret = os.environ.get(fleet.secret_env, "").strip()
    if secret:
        headers["X-Caretaker-Signature"] = "sha256=" + sign_payload(body, secret)

    owns_client = client is None
    try:
        if owns_client:
            client = httpx.AsyncClient(timeout=fleet.timeout_seconds)
        assert client is not None  # narrow for type-checkers
        try:
            oauth_headers = await _oauth_bearer_headers(fleet, client=client)
            if oauth_headers:
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
