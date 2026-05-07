from __future__ import annotations

import json
import logging
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from caretaker.coding_jobs.models import CodingJobMessage
    from caretaker.config import CodingJobsConfig

logger = logging.getLogger(__name__)


class K8sJobOutcome(StrEnum):
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    TIMEOUT = "TIMEOUT"
    NOT_FOUND = "NOT_FOUND"


class K8sJobSpawner:
    def __init__(self, *, batch_api: Any, config: CodingJobsConfig) -> None:
        self._api = batch_api
        self._config = config

    def spawn(self, msg: CodingJobMessage) -> str:
        """Create a K8s Job. Returns the job name. 409 = already exists, skipped safely."""
        from kubernetes import client as k8s
        from kubernetes.client.exceptions import ApiException

        # TASK_PAYLOAD carries the minimal body only — same shape as ASB message body
        task_payload = json.dumps(msg.to_asb_body())

        env = [
            k8s.V1EnvVar(name="JOB_ID", value=msg.job_id),
            k8s.V1EnvVar(name="ATTEMPT", value=str(msg.attempt)),
            k8s.V1EnvVar(name="TASK_PAYLOAD", value=task_payload),
            k8s.V1EnvVar(name="STATUS_STREAM", value=self._config.stream_job_status),
            k8s.V1EnvVar(
                name="REDIS_URL",
                value_from=k8s.V1EnvVarSource(
                    secret_key_ref=k8s.V1SecretKeySelector(
                        name="caretaker-secrets", key="redis-url", optional=True
                    )
                ),
            ),
            k8s.V1EnvVar(
                name="ANTHROPIC_API_KEY",
                value_from=k8s.V1EnvVarSource(
                    secret_key_ref=k8s.V1SecretKeySelector(
                        name="caretaker-secrets", key="anthropic-api-key", optional=True
                    )
                ),
            ),
            k8s.V1EnvVar(
                name="GITHUB_TOKEN",
                value_from=k8s.V1EnvVarSource(
                    secret_key_ref=k8s.V1SecretKeySelector(
                        name="caretaker-secrets", key="github-app-installation-token"
                    )
                ),
            ),
        ]

        job = k8s.V1Job(
            api_version="batch/v1",
            kind="Job",
            metadata=k8s.V1ObjectMeta(
                name=msg.k8s_name,
                namespace=self._config.k8s_namespace,
                labels={"app": "caretaker-coding-worker", "job-id": msg.job_id},
            ),
            spec=k8s.V1JobSpec(
                template=k8s.V1PodTemplateSpec(
                    metadata=k8s.V1ObjectMeta(labels={"app": "caretaker-coding-worker"}),
                    spec=k8s.V1PodSpec(
                        restart_policy="Never",
                        service_account_name="caretaker-agent-worker",
                        security_context=k8s.V1PodSecurityContext(
                            run_as_non_root=True, run_as_user=1001
                        ),
                        containers=[
                            k8s.V1Container(
                                name="coding-worker",
                                image=self._config.k8s_worker_image,
                                image_pull_policy="Always",
                                args=["python", "-m", "caretaker.coding_jobs.worker"],
                                env=env,
                                resources=k8s.V1ResourceRequirements(
                                    requests={"cpu": "500m", "memory": "1Gi"},
                                    limits={"cpu": "2000m", "memory": "4Gi"},
                                ),
                                security_context=k8s.V1SecurityContext(
                                    allow_privilege_escalation=False,
                                    capabilities=k8s.V1Capabilities(drop=["ALL"]),
                                ),
                            )
                        ],
                        volumes=[
                            k8s.V1Volume(
                                name="workspace",
                                empty_dir=k8s.V1EmptyDirVolumeSource(size_limit="4Gi"),
                            )
                        ],
                    ),
                ),
                backoff_limit=0,
                active_deadline_seconds=self._config.per_attempt_timeout_secs,
                ttl_seconds_after_finished=600,
            ),
        )

        try:
            self._api.create_namespaced_job(namespace=self._config.k8s_namespace, body=job)
            logger.info("k8s: created job name=%s job_id=%s", msg.k8s_name, msg.job_id)
        except Exception as exc:
            from kubernetes.client.exceptions import ApiException

            if isinstance(exc, ApiException) and exc.status == 409:
                logger.info("k8s: job already exists name=%s — skipping", msg.k8s_name)
            else:
                raise
        return msg.k8s_name

    def get_status(self, job_name: str) -> K8sJobOutcome:
        from kubernetes.client.exceptions import ApiException

        try:
            status = self._api.read_namespaced_job_status(
                name=job_name, namespace=self._config.k8s_namespace
            )
        except ApiException as exc:
            if exc.status == 404:
                return K8sJobOutcome.NOT_FOUND
            raise

        js = status.status
        for cond in js.conditions or []:
            if cond.type == "DeadlineExceeded" and cond.status == "True":
                return K8sJobOutcome.TIMEOUT
        if js.succeeded and js.succeeded > 0:
            return K8sJobOutcome.SUCCEEDED
        if js.failed and js.failed > 0:
            return K8sJobOutcome.FAILED
        return K8sJobOutcome.RUNNING

    def list_coding_job_names(self) -> list[str]:
        result = self._api.list_namespaced_job(
            namespace=self._config.k8s_namespace,
            label_selector="app=caretaker-coding-worker",
        )
        return [item.metadata.name for item in result.items]


def build_spawner(config: CodingJobsConfig) -> K8sJobSpawner:
    from kubernetes import client as k8s_client
    from kubernetes import config as k8s_config

    try:
        k8s_config.load_incluster_config()
    except Exception:
        k8s_config.load_kube_config()
    return K8sJobSpawner(batch_api=k8s_client.BatchV1Api(), config=config)


__all__ = ["K8sJobOutcome", "K8sJobSpawner", "build_spawner"]
