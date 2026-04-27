"""Backend-side reconciliation scheduler.

Replaces the per-repo cron in the heavy workflow template
(``.github/workflows/maintainer.yml``) with a single in-cluster schedule.
On each tick, fans out a synthetic ``schedule`` event onto the event
bus per installed repo so the existing dispatcher / consumer path runs
the full agent suite.

A Redis-backed lease ensures only one replica fans out per tick, so
multi-pod deployments do not multiply the workload.
"""

from caretaker.scheduler.reconciliation import (
    ReconciliationScheduler,
    start_reconciliation_scheduler,
)

__all__ = [
    "ReconciliationScheduler",
    "start_reconciliation_scheduler",
]
