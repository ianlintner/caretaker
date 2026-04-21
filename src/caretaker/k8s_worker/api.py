"""Admin API surface for the Kubernetes agent worker.

Mounted under ``/api/admin/agent-tasks`` by the MCP backend when the
``k8s_worker`` config block is enabled. Endpoints require the usual
OIDC session; the launcher itself handles namespace / RBAC concerns.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from caretaker.k8s_worker.launcher import K8sAgentLauncher, K8sLauncherError

if TYPE_CHECKING:
    from caretaker.config import K8sAgentWorkerConfig

logger = logging.getLogger(__name__)


router = APIRouter(prefix="/api/admin/agent-tasks", tags=["agent-tasks"])


# Module-level singleton resolved by the MCP backend during startup.
_launcher: K8sAgentLauncher | None = None
_config: K8sAgentWorkerConfig | None = None


def configure(launcher: K8sAgentLauncher, config: K8sAgentWorkerConfig) -> None:
    """Wire the launcher + config. Called once during MCP backend startup."""
    global _launcher, _config  # noqa: PLW0603
    _launcher = launcher
    _config = config


def _require_launcher() -> K8sAgentLauncher:
    if _launcher is None:
        raise HTTPException(status_code=503, detail="Kubernetes agent worker not configured")
    return _launcher


def _auth_dependency() -> Any:
    """Lazy-load the admin session dependency so fleet-less deployments
    that don't mount this router don't pay the import cost."""
    from caretaker.admin.auth import require_session

    return Depends(require_session)


_REQUIRE_SESSION = _auth_dependency()


class AgentTaskRequest(BaseModel):
    """Payload for a manual admin-side dispatch."""

    repo: str = Field(description="owner/name of the target repository")
    issue_number: int = Field(gt=0, description="Issue or PR number to operate on")
    task_type: str = Field(
        default="LINT_FAILURE",
        description="CodingTask type (matches foundry allowlist entries)",
    )
    image: str | None = Field(
        default=None,
        description="Container image override. Defaults to config.image.",
    )


@router.post("")
async def create_agent_task(
    req: AgentTaskRequest,
    _user: Any = _REQUIRE_SESSION,
) -> dict[str, Any]:
    """Spawn (or re-use a deduped) Kubernetes Job for a coding task."""
    launcher = _require_launcher()
    try:
        record = await launcher.dispatch(
            repo=req.repo,
            issue_number=req.issue_number,
            task_type=req.task_type,
            image=req.image,
        )
    except K8sLauncherError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Agent task dispatch failed")
        raise HTTPException(status_code=500, detail=f"dispatch failed: {exc}") from exc
    return record.to_dict()


@router.get("")
async def list_agent_tasks(
    limit: int = Query(default=50, ge=1, le=200),
    _user: Any = _REQUIRE_SESSION,
) -> dict[str, Any]:
    """List recent agent-worker Jobs in the configured namespace."""
    launcher = _require_launcher()
    try:
        items = await launcher.list_recent(limit=limit)
    except K8sLauncherError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"items": items, "total": len(items)}


__all__ = ["configure", "router"]
