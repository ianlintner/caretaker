"""Streamed-run lifecycle: workflow OIDC → backend execution → SSE fan-out.

This package implements the streaming connection between consumer GitHub
Actions workflows and the caretaker backend. Workflows authenticate via
GitHub OIDC, register a *run* up front, ask the backend to execute the
agents on their behalf (offloading work from the runner), and tail the
resulting log stream — which the admin UI can also subscribe to.

Modules:

* :mod:`caretaker.runs.models` — pydantic models (``RunRecord``, ``LogEntry``).
* :mod:`caretaker.runs.store` — Redis Streams + Mongo archive wrapper.
* :mod:`caretaker.runs.api` — FastAPI router for ``POST /runs/*``.
* :mod:`caretaker.runs.tokens` — HMAC-signed ``ingest_token`` issuance.
* :mod:`caretaker.runs.shipper` — runner-side CLI client (``caretaker stream``).
* :mod:`caretaker.runs.oidc_minter` — runner-side helper that exchanges
  the ``ACTIONS_ID_TOKEN_REQUEST_*`` env vars for a backend-bound JWT.
"""

from caretaker.runs.models import LogEntry, RunRecord, RunStatus

__all__ = ["LogEntry", "RunRecord", "RunStatus"]
