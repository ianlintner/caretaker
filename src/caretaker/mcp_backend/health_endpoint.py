"""Helpers for the ``/health/deep`` FastAPI endpoint in :mod:`main`.

The endpoint itself does many things — load config, parse env for three
datastores, build optional clients, shape a JSON response — and would
otherwise dominate ``main.py``. Pulling that wiring here keeps the
FastAPI handler ~25 LoC and makes the moving parts unit-testable.

Two pieces matter:

* :func:`resolve_models` parses ``MaintainerConfig`` and returns the set of
  models the PR-reviewer plans to use, the ``opencode`` binary path, and a
  structured ``config`` check that surfaces YAML failures at the top level
  instead of silently degrading the model probe.
* :func:`build_clients` is an :class:`AsyncExitStack`-based async context
  manager that constructs Mongo + Neo4j clients on demand and *registers
  their closers at construction time*, so an unexpected exception between
  build and gather can never leak a TCP connection.
"""

from __future__ import annotations

import logging
import os
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

logger = logging.getLogger(__name__)


# ── Config / model resolution ────────────────────────────────────────────


@dataclass(frozen=True)
class ResolvedModels:
    """Output of :func:`resolve_models`."""

    models: list[str]
    """Deduplicated list of model IDs to probe; empty when the config is
    broken or when no review models are configured."""

    opencode_cli_path: str
    """Path to the ``opencode`` CLI binary (used by the model probe)."""

    config_check: dict[str, Any]
    """``{"status": "ok"}`` on success, ``{"status": "fail", "error": ...}``
    when ``MaintainerConfig.from_yaml`` raises. Plumbed into
    ``gather_deep_health`` as a CORE check so a bad YAML returns 503."""


def resolve_models() -> ResolvedModels:
    """Load ``MaintainerConfig`` and return the model probe inputs.

    Reads ``CARETAKER_CONFIG_PATH`` (default ``.github/maintainer/config.yml``).

    On parse failure: returns an empty model list, the default ``opencode``
    binary path, and a ``fail`` config check so the rollup turns 503.
    """
    # Local import keeps module import cheap for code paths that don't hit
    # /health/deep (e.g. the cold-start health probe).
    from caretaker.config import MaintainerConfig
    from caretaker.observability.health import collect_models_to_probe

    cfg_path = os.environ.get("CARETAKER_CONFIG_PATH", ".github/maintainer/config.yml")
    try:
        cfg = MaintainerConfig.from_yaml(cfg_path)
    except FileNotFoundError:
        # No file is a valid backend-deployment state — the MCP pod uses
        # defaults + env vars, not a mounted YAML. Treat as ok (with a
        # ``note`` so the operator can still see what's going on) instead
        # of fail. A malformed YAML is still a hard fail (next branch).
        logger.info(
            "MaintainerConfig file %s not present; /health/deep using defaults",
            cfg_path,
        )
        cfg = MaintainerConfig()
        models = collect_models_to_probe(
            opencode_local=cfg.pr_reviewer.opencode_local,
            llm=cfg.llm,
        )
        opencode_cli_path = cfg.pr_reviewer.opencode_local.cli_path
        return ResolvedModels(
            models=models,
            opencode_cli_path=opencode_cli_path,
            config_check={
                "status": "ok",
                "note": f"config file {cfg_path} not present; using defaults",
            },
        )
    except Exception as exc:  # noqa: BLE001 — yaml + pydantic raise heterogeneous errors
        logger.exception("Failed to load MaintainerConfig for /health/deep")
        return ResolvedModels(
            models=[],
            opencode_cli_path="opencode",
            config_check={
                "status": "fail",
                "error": f"{type(exc).__name__}: {exc}",
            },
        )

    try:
        models = collect_models_to_probe(
            opencode_local=cfg.pr_reviewer.opencode_local,
            llm=cfg.llm,
        )
        opencode_cli_path = cfg.pr_reviewer.opencode_local.cli_path
    except Exception as exc:  # noqa: BLE001 — defensive against config drift
        logger.exception("Failed to resolve models from MaintainerConfig")
        return ResolvedModels(
            models=[],
            opencode_cli_path="opencode",
            config_check={
                "status": "fail",
                "error": f"{type(exc).__name__}: {exc}",
            },
        )

    return ResolvedModels(
        models=models,
        opencode_cli_path=opencode_cli_path,
        config_check={"status": "ok"},
    )


# ── Client bundle (Mongo + Neo4j + Redis URL) ────────────────────────────


@dataclass
class ClientBundle:
    """Optional clients + the resolved Redis URL for one /health/deep call.

    Mongo and Neo4j are ``None`` when the corresponding env var is unset or
    the client constructor raised; ``check_mongo`` / ``check_neo4j`` map
    that to a ``"not configured"`` failure record.
    """

    mongo_client: Any
    neo4j_driver: Any
    redis_url: str


def _resolve_redis_url() -> str:
    """Pick the Redis URL from the env, falling back to the in-cluster default."""
    return (
        os.environ.get("REDIS_URL", "").strip()
        or os.environ.get("CARETAKER_REDIS_URL", "").strip()
        or "redis://redis:6379"
    )


def _parse_neo4j_auth(raw_auth: str) -> tuple[str, str]:
    """``"user/pass"`` → ``("user", "pass")``; default user when no slash."""
    parts = raw_auth.split("/", 1)
    if len(parts) == 2:
        return (parts[0], parts[1])
    return ("neo4j", raw_auth)


@asynccontextmanager
async def build_clients() -> AsyncIterator[ClientBundle]:
    """Build Mongo + Neo4j clients with eager-registered cleanup.

    Uses :class:`AsyncExitStack` so each client's closer is registered the
    moment it's constructed — closing the window where an exception between
    construction and the ``await gather`` could leak the connection. Mongo's
    ``AsyncIOMotorClient.close()`` is synchronous; Neo4j's
    ``AsyncDriver.close()`` is awaitable; the stack handles both.
    """
    redis_url = _resolve_redis_url()

    async with AsyncExitStack() as stack:
        mongo_client: Any = None
        mongo_url = os.environ.get("MONGODB_URL", "").strip()
        if mongo_url:
            try:
                from motor.motor_asyncio import AsyncIOMotorClient

                mongo_client = AsyncIOMotorClient(mongo_url, serverSelectionTimeoutMS=5000)
                # Motor's close is synchronous (no aclose in the versions we
                # ship). push_callback registers it for stack unwind.
                stack.callback(mongo_client.close)
            except Exception:  # noqa: BLE001
                logger.debug(
                    "Failed to construct mongo client for /health/deep",
                    exc_info=True,
                )
                mongo_client = None

        neo4j_driver: Any = None
        neo4j_url = os.environ.get("NEO4J_URL", "").strip()
        if neo4j_url:
            try:
                import neo4j

                raw_auth = os.environ.get("NEO4J_AUTH", "neo4j/neo4j")
                auth = _parse_neo4j_auth(raw_auth)
                neo4j_driver = neo4j.AsyncGraphDatabase.driver(neo4j_url, auth=auth)
                # Neo4j's AsyncDriver.close is awaitable.
                stack.push_async_callback(neo4j_driver.close)
            except Exception:  # noqa: BLE001
                logger.debug(
                    "Failed to construct neo4j driver for /health/deep",
                    exc_info=True,
                )
                neo4j_driver = None

        yield ClientBundle(
            mongo_client=mongo_client,
            neo4j_driver=neo4j_driver,
            redis_url=redis_url,
        )
