"""GitHub App front-end scaffolding for caretaker (Phase 1).

See ``docs/github-app-plan.md`` for the overall design.  This package is a
superset of pure utilities — JWT signing, installation token minting,
webhook signature verification, and event → agent mapping — plus the
credential-provider implementation that plugs into
``caretaker.github_client.credentials.GitHubCredentialsProvider``.

The package is deliberately small and has no dependency on the rest of
caretaker at import time so it can be consumed from both the orchestrator
and the FastAPI webhook receiver without circular imports.
"""

from __future__ import annotations

from .dispatch_guard import (
    DispatchEvent,
    DispatchVerdict,
    evaluate_dispatch,
    judge_dispatch,
    judge_dispatch_llm,
    legacy_dispatch_verdict,
)
from .events import (
    EVENT_AGENT_MAP,
    agents_for_event,
    normalize_event_name,
)
from .installation_tokens import (
    InstallationToken,
    InstallationTokenCache,
    InstallationTokenMinter,
)
from .jwt_signer import AppJWTSigner
from .provider import GitHubAppCredentialsProvider
from .webhooks import (
    WebhookSignatureError,
    parse_webhook,
    verify_signature,
)

__all__ = [
    "EVENT_AGENT_MAP",
    "AppJWTSigner",
    "DispatchEvent",
    "DispatchVerdict",
    "GitHubAppCredentialsProvider",
    "InstallationToken",
    "InstallationTokenCache",
    "InstallationTokenMinter",
    "WebhookSignatureError",
    "agents_for_event",
    "evaluate_dispatch",
    "judge_dispatch",
    "judge_dispatch_llm",
    "legacy_dispatch_verdict",
    "normalize_event_name",
    "parse_webhook",
    "verify_signature",
]
