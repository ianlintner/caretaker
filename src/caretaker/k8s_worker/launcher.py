"""Kubernetes Job launcher for the custom coding agent.

Phase 3 rollout surface from ``docs/custom-coding-agent-plan.md``:
the MCP backend clones the template Job manifest (see
``infra/k8s/caretaker-agent-worker.yaml``) per dispatch, overrides the
target repo / issue env vars, and POSTs to the Kubernetes API. The
resulting pod runs caretaker's existing custom executor end-to-end and
exits.

The launcher is designed to be *fail-safe*:

* The ``kubernetes`` Python package is an optional dependency (extras
  group ``k8s-worker``). If it isn't installed, every launcher method
  raises :class:`K8sLauncherError` instead of ImportError — callers
  (the admin endpoint) then return a structured 503.
* Optional Redis dedupe. When an async Redis client is provided and a
  dispatch for ``(repo, issue_number)`` already exists within
  ``dedupe_ttl_seconds``, the launcher returns the cached Job name
  without creating a second pod. Prevents a duplicate-dispatch storm if
  the admin UI retries mid-request.
* The launcher never touches the Kubernetes API directly in a unit
  test — ``build_job_manifest`` is a pure function over the config +
  dispatch payload, so tests exercise the full synthesis path without
  requiring a cluster.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from caretaker.observability.metrics import (
    record_worker_job,
    set_worker_queue_depth,
)

if TYPE_CHECKING:
    from caretaker.config import K8sAgentWorkerConfig

    # Imported lazily inside ``_ensure_batch_api`` / typed as ``Any`` in
    # parameter lists so we don't pin a generic arity the Redis client
    # doesn't expose consistently across versions.
    AsyncRedis = Any  # noqa: F401

logger = logging.getLogger(__name__)


class K8sLauncherError(RuntimeError):
    """Raised when the launcher cannot create or observe a Job."""


@dataclass
class DispatchRecord:
    """Return value of :meth:`K8sAgentLauncher.dispatch`."""

    job_name: str
    namespace: str
    repo: str
    issue_number: int
    task_type: str
    created_at: datetime
    # When True, the launcher found an existing Job in the dedupe
    # window and returned that Job's name instead of creating a new one.
    deduped: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_name": self.job_name,
            "namespace": self.namespace,
            "repo": self.repo,
            "issue_number": self.issue_number,
            "task_type": self.task_type,
            "created_at": self.created_at.isoformat(),
            "deduped": self.deduped,
        }


_DNS_LABEL_SAFE = re.compile(r"[^a-z0-9-]")


def _slugify(value: str, *, max_len: int = 24) -> str:
    """Kubernetes-safe DNS-1123 label fragment."""
    lowered = value.lower().replace("_", "-").replace("/", "-")
    safe = _DNS_LABEL_SAFE.sub("-", lowered)
    trimmed = re.sub(r"-+", "-", safe).strip("-")
    return trimmed[:max_len] or "caretaker"


def build_job_name(*, name_prefix: str, repo: str, issue_number: int, task_type: str) -> str:
    """Stable, collision-resistant Job name.

    We hash ``(repo, issue_number, task_type, utc-minute)`` to keep the
    name deterministic within a minute (so concurrent calls land on the
    same Job name and Kubernetes' create-if-not-exists semantics do the
    de-duplication for us) while still rotating once per minute so
    retries after an earlier failure aren't blocked.
    """
    minute_bucket = datetime.now(UTC).strftime("%Y%m%d%H%M")
    digest_input = f"{repo}#{issue_number}|{task_type}|{minute_bucket}".encode()
    short = hashlib.sha1(digest_input, usedforsecurity=False).hexdigest()[:8]
    slug = _slugify(f"{repo.split('/')[-1]}-{issue_number}-{task_type}")
    # Stay under the 63-char DNS-1123 label cap.
    base = f"{name_prefix}-{slug}"
    head = base[: 63 - len(short) - 1]
    return f"{head}-{short}"


def build_job_manifest(
    *,
    config: K8sAgentWorkerConfig,
    repo: str,
    issue_number: int,
    task_type: str,
    image: str | None = None,
    extra_env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Assemble a Job manifest dict from the dispatch payload.

    Pure function — no Kubernetes client dependency. ``K8sAgentLauncher``
    passes the returned dict to ``BatchV1Api.create_namespaced_job``.
    """
    name = build_job_name(
        name_prefix=config.name_prefix,
        repo=repo,
        issue_number=issue_number,
        task_type=task_type,
    )
    env_overrides = {
        "GITHUB_REPOSITORY": repo,
        "CARETAKER_TARGET_NUMBER": str(issue_number),
        "CARETAKER_EVENT_TYPE": "issues",
        "CARETAKER_TASK_TYPE": task_type,
    }
    if extra_env:
        env_overrides.update(extra_env)
    container_env = [{"name": k, "value": v} for k, v in env_overrides.items()]

    container_image = image or config.image or "CARETAKER_IMAGE_NOT_CONFIGURED"

    labels = {
        "app": "caretaker-agent-worker",
        "caretaker.io/repo": _slugify(repo, max_len=63),
        "caretaker.io/issue": str(issue_number),
        "caretaker.io/task-type": _slugify(task_type, max_len=63),
    }

    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": name,
            "namespace": config.namespace,
            "labels": labels,
            "annotations": {
                "caretaker.io/template": config.template_job_name,
                "caretaker.io/created-at": datetime.now(UTC).isoformat(),
            },
        },
        "spec": {
            "ttlSecondsAfterFinished": config.ttl_seconds_after_finished,
            "backoffLimit": 0,
            "activeDeadlineSeconds": config.active_deadline_seconds,
            "template": {
                "metadata": {"labels": labels},
                "spec": {
                    "serviceAccountName": config.service_account,
                    "restartPolicy": "Never",
                    "securityContext": {
                        "runAsNonRoot": True,
                        "runAsUser": 1001,
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                    "containers": [
                        {
                            "name": "agent",
                            "image": container_image,
                            "imagePullPolicy": "Always",
                            "args": [
                                "caretaker",
                                "run",
                                "--config",
                                "/workspace/.github/maintainer/config.yml",
                                "--mode",
                                "full",
                            ],
                            "env": container_env,
                            "securityContext": {
                                "allowPrivilegeEscalation": False,
                                "readOnlyRootFilesystem": True,
                                "capabilities": {"drop": ["ALL"]},
                            },
                            "resources": {
                                "requests": {"cpu": "250m", "memory": "512Mi"},
                                "limits": {"cpu": "1000m", "memory": "1Gi"},
                            },
                            "volumeMounts": [
                                {"name": "workspace", "mountPath": "/workspace"},
                                {"name": "tmp", "mountPath": "/tmp"},
                            ],
                        }
                    ],
                    "volumes": [
                        {"name": "workspace", "emptyDir": {"sizeLimit": "2Gi"}},
                        {"name": "tmp", "emptyDir": {"sizeLimit": "256Mi"}},
                    ],
                },
            },
        },
    }


def _dedupe_key(repo: str, issue_number: int, task_type: str) -> str:
    return f"caretaker:agent-dispatch:{repo}#{issue_number}:{task_type}"


class K8sAgentLauncher:
    """Creates ``batch/v1 Job`` resources in the caretaker namespace."""

    def __init__(
        self,
        *,
        config: K8sAgentWorkerConfig,
        redis: Any | None = None,
    ) -> None:
        self._config = config
        self._redis = redis
        self._batch_api: Any | None = None

    @property
    def config(self) -> K8sAgentWorkerConfig:
        return self._config

    def _ensure_batch_api(self) -> Any:
        """Lazily import and initialise the Kubernetes BatchV1Api client."""
        if self._batch_api is not None:
            return self._batch_api
        try:
            from kubernetes import client  # type: ignore[import-not-found]
            from kubernetes import config as kube_config
        except ImportError as exc:
            raise K8sLauncherError(
                "Kubernetes worker requires the `kubernetes` package "
                "(install the `k8s-worker` extra)."
            ) from exc
        try:
            kube_config.load_incluster_config()
        except Exception:  # pragma: no cover — local dev fallback
            try:
                kube_config.load_kube_config()
            except Exception as exc:
                raise K8sLauncherError(f"Unable to load Kubernetes config: {exc}") from exc
        self._batch_api = client.BatchV1Api()
        return self._batch_api

    async def dispatch(
        self,
        *,
        repo: str,
        issue_number: int,
        task_type: str,
        image: str | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> DispatchRecord:
        """Create (or re-use a deduped) Job for a single coding task."""
        _start = time.perf_counter()
        _outcome = "success"
        if not self._config.enabled:
            _outcome = "failure"
            with contextlib.suppress(Exception):  # pragma: no cover
                record_worker_job(
                    job="k8s-agent-worker-dispatch",
                    outcome=_outcome,
                    duration=time.perf_counter() - _start,
                )
            raise K8sLauncherError("k8s_worker.enabled is False")
        if not repo or "/" not in repo:
            raise K8sLauncherError(f"repo must be in 'owner/name' form (got {repo!r})")
        if issue_number <= 0:
            raise K8sLauncherError(f"issue_number must be positive (got {issue_number})")

        # 1. Redis-backed dedupe (if configured + reachable).
        if self._redis is not None and self._config.dedupe_ttl_seconds > 0:
            key = _dedupe_key(repo, issue_number, task_type)
            try:
                existing = await self._redis.get(key)
            except Exception as exc:
                logger.warning("k8s dispatch dedupe lookup failed: %s", exc)
                existing = None
            if existing:
                existing_name = (
                    existing.decode("utf-8")
                    if isinstance(existing, (bytes, bytearray))
                    else str(existing)
                )
                logger.info(
                    "k8s dispatch deduped: %s → existing Job %s",
                    key,
                    existing_name,
                )
                return DispatchRecord(
                    job_name=existing_name,
                    namespace=self._config.namespace,
                    repo=repo,
                    issue_number=issue_number,
                    task_type=task_type,
                    created_at=datetime.now(UTC),
                    deduped=True,
                )

        # 2. Build manifest + create.
        manifest = build_job_manifest(
            config=self._config,
            repo=repo,
            issue_number=issue_number,
            task_type=task_type,
            image=image,
            extra_env=extra_env,
        )

        api = self._ensure_batch_api()
        try:
            api.create_namespaced_job(namespace=self._config.namespace, body=manifest)
        except Exception as exc:
            raise K8sLauncherError(f"create_namespaced_job failed: {exc}") from exc

        record = DispatchRecord(
            job_name=manifest["metadata"]["name"],
            namespace=self._config.namespace,
            repo=repo,
            issue_number=issue_number,
            task_type=task_type,
            created_at=datetime.now(UTC),
            deduped=False,
        )

        # 3. Persist dedupe pointer.
        if self._redis is not None and self._config.dedupe_ttl_seconds > 0:
            try:
                await self._redis.set(
                    _dedupe_key(repo, issue_number, task_type),
                    record.job_name,
                    ex=self._config.dedupe_ttl_seconds,
                )
            except Exception as exc:
                logger.warning("k8s dispatch dedupe write failed: %s", exc)

        logger.info(
            "k8s dispatch created: Job %s/%s for %s#%d (%s)",
            record.namespace,
            record.job_name,
            repo,
            issue_number,
            task_type,
        )
        with contextlib.suppress(Exception):  # pragma: no cover
            record_worker_job(
                job="k8s-agent-worker-dispatch",
                outcome="success",
                duration=time.perf_counter() - _start,
            )
        return record

    async def list_recent(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """Return recent Jobs in the worker namespace, newest first."""
        api = self._ensure_batch_api()
        try:
            jobs = api.list_namespaced_job(
                namespace=self._config.namespace,
                label_selector="app=caretaker-agent-worker",
                limit=limit,
            )
        except Exception as exc:
            raise K8sLauncherError(f"list_namespaced_job failed: {exc}") from exc

        items: list[dict[str, Any]] = []
        for job in getattr(jobs, "items", []) or []:
            meta = getattr(job, "metadata", None)
            status = getattr(job, "status", None)
            creation_ts = getattr(meta, "creation_timestamp", None)
            created_at = (
                creation_ts.isoformat()
                if creation_ts is not None and hasattr(creation_ts, "isoformat")
                else None
            )
            items.append(
                {
                    "name": getattr(meta, "name", None),
                    "namespace": getattr(meta, "namespace", None),
                    "labels": dict(getattr(meta, "labels", {}) or {}),
                    "created_at": created_at,
                    "active": getattr(status, "active", None),
                    "succeeded": getattr(status, "succeeded", None),
                    "failed": getattr(status, "failed", None),
                }
            )
        items.sort(key=lambda r: r.get("created_at") or "", reverse=True)

        # Publish the live "pending + active" depth for
        # ``worker_queue_depth{queue="caretaker-agent-worker"}``. We
        # consider a Job "in-flight" when its ``active`` count is set
        # (running pod) or when ``succeeded`` and ``failed`` are both
        # absent (queued, not yet started).
        try:
            active = sum(
                1
                for item in items
                if (item.get("active") or 0) > 0
                or (item.get("succeeded") is None and item.get("failed") is None)
            )
            set_worker_queue_depth("caretaker-agent-worker", active)
        except Exception:  # pragma: no cover
            pass

        return items
