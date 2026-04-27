"""GitHub Actions OIDC bearer-token verifier.

Workflows running on GitHub-hosted runners can mint a short-lived OIDC JWT
signed by ``https://token.actions.githubusercontent.com`` and present it to
the caretaker backend in place of any long-lived secret. This module wires
that issuer into :mod:`caretaker.auth.bearer` and provides an
:class:`ActionsPrincipal` extracted from the verified token plus a FastAPI
dependency factory :func:`require_actions_principal` that:

* asserts the resolved principal came from the GitHub Actions issuer;
* extracts the workflow context claims (``repository``, ``run_id``, …);
* (optionally) verifies the caretaker GitHub App is installed for the
  caller's repository — rejecting tokens from repos that do not have the
  App installed even if the JWT signature is otherwise valid.

The GitHub OIDC issuer mints tokens with the following claims (subset)::

    iss          https://token.actions.githubusercontent.com
    aud          <whatever audience the workflow asks for>
    sub          repo:<owner>/<repo>:ref:refs/heads/<branch>
    repository   owner/repo
    repository_id   "12345"
    repository_owner   owner
    repository_owner_id   "67890"
    run_id       "9876543210"
    run_attempt  "1"
    actor        ianlintner
    actor_id     "12345"
    event_name   workflow_dispatch
    ref          refs/heads/main
    sha          deadbeef…
    workflow     Caretaker Maintainer
    job_workflow_ref   owner/repo/.github/workflows/maintainer.yml@refs/heads/main

We pin ``aud`` to the value configured in the backend so that a token minted
for some other service can never be replayed against the caretaker API.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException, Request, status

from caretaker.auth import bearer

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)


GITHUB_ACTIONS_ISSUER = "https://token.actions.githubusercontent.com"


@dataclass(frozen=True)
class ActionsPrincipal:
    """Authenticated GitHub Actions workflow caller.

    Constructed from a verified OIDC JWT — every field has been signature-
    validated by GitHub's JWKS, so the workflow cannot lie about which
    repository it represents.
    """

    repository: str  # "owner/repo"
    repository_id: int
    repository_owner: str
    repository_owner_id: int
    run_id: int
    run_attempt: int
    actor: str
    event_name: str
    ref: str
    sha: str
    workflow: str
    job_workflow_ref: str
    sub: str
    raw_claims: dict[str, Any]


def _coerce_int(claims: dict[str, Any], key: str) -> int:
    value = claims.get(key)
    if value is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"GitHub OIDC token missing claim: {key}",
        )
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"GitHub OIDC token claim {key!r} is not an integer: {value!r}",
        ) from exc


def _coerce_str(claims: dict[str, Any], key: str) -> str:
    value = claims.get(key)
    if not isinstance(value, str) or not value:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"GitHub OIDC token missing string claim: {key}",
        )
    return value


def principal_from_claims(claims: dict[str, Any]) -> ActionsPrincipal:
    """Build :class:`ActionsPrincipal` from a verified GitHub OIDC JWT's claims.

    Raises ``HTTPException(401)`` if any required claim is missing or
    malformed. The signature must already have been verified by
    :mod:`caretaker.auth.bearer` before calling this.
    """
    return ActionsPrincipal(
        repository=_coerce_str(claims, "repository"),
        repository_id=_coerce_int(claims, "repository_id"),
        repository_owner=_coerce_str(claims, "repository_owner"),
        repository_owner_id=_coerce_int(claims, "repository_owner_id"),
        run_id=_coerce_int(claims, "run_id"),
        run_attempt=_coerce_int(claims, "run_attempt"),
        actor=_coerce_str(claims, "actor"),
        event_name=_coerce_str(claims, "event_name"),
        ref=claims.get("ref", "") if isinstance(claims.get("ref"), str) else "",
        sha=claims.get("sha", "") if isinstance(claims.get("sha"), str) else "",
        workflow=claims.get("workflow", "") if isinstance(claims.get("workflow"), str) else "",
        job_workflow_ref=(
            claims.get("job_workflow_ref", "")
            if isinstance(claims.get("job_workflow_ref"), str)
            else ""
        ),
        sub=_coerce_str(claims, "sub"),
        raw_claims=claims,
    )


async def configure_github_oidc(
    *,
    audience: str,
    leeway_seconds: int = 30,
) -> None:
    """Register the GitHub Actions OIDC issuer with :mod:`caretaker.auth.bearer`.

    ``audience`` MUST match what consumer workflows pass when minting their
    OIDC token (e.g. ``caretaker-backend``). The check is signature-bound:
    GitHub embeds the requested audience in the signed claim, so a token
    minted for another service cannot be replayed here.
    """
    if not audience:
        raise ValueError("GitHub OIDC requires a non-empty audience")
    await bearer.configure(
        issuer_url=GITHUB_ACTIONS_ISSUER,
        audience=audience,
        leeway_seconds=leeway_seconds,
    )


def is_configured() -> bool:
    return bearer.is_issuer_configured(GITHUB_ACTIONS_ISSUER)


# Type for the optional "App-installed" gate. The dispatcher / token broker
# resolves ``owner/repo`` → installation_id when the GitHub App is configured
# for the caretaker backend; that lookup is the cheapest authorization gate
# we can apply ("only repos that installed the App may post runs"). The
# function returns the installation id, or None if the App is not installed.
InstallationCheck = "Callable[[str], Awaitable[int | None]]"


_installation_check: Any = None


def set_installation_check(check: Any) -> None:
    """Wire an async ``(repository: str) -> installation_id | None`` callback.

    When set, :func:`require_actions_principal` rejects tokens for repos
    that do not have the caretaker App installed. When unset (e.g. local
    dev without the App configured), no installation check runs.
    """
    global _installation_check  # noqa: PLW0603
    _installation_check = check


def reset() -> None:
    """Clear module state (for tests)."""
    global _installation_check  # noqa: PLW0603
    _installation_check = None


def require_actions_principal(
    *,
    require_app_installation: bool = True,
) -> Callable[[Request], Awaitable[ActionsPrincipal]]:
    """FastAPI dependency factory returning an :class:`ActionsPrincipal`.

    Verifies the bearer JWT, asserts ``iss == GITHUB_ACTIONS_ISSUER``, and
    optionally rejects tokens whose repository does not have the caretaker
    App installed.
    """

    async def _dependency(request: Request) -> ActionsPrincipal:
        # Re-use the multi-issuer bearer dependency without scope checks.
        principal = await bearer.require_bearer_token()(request)
        if principal.issuer != GITHUB_ACTIONS_ISSUER:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=(f"Token issuer {principal.issuer!r} is not the GitHub Actions OIDC issuer"),
                headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
            )
        actions = principal_from_claims(principal.raw_claims)

        if require_app_installation and _installation_check is not None:
            try:
                installation_id = await _installation_check(actions.repository)
            except Exception:  # pragma: no cover — defensive
                logger.exception(
                    "Installation check raised for repo=%s; rejecting",
                    actions.repository,
                )
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="GitHub App installation lookup failed",
                ) from None
            if not installation_id:
                logger.warning(
                    "Rejecting OIDC token for repo=%s: caretaker App not installed",
                    actions.repository,
                )
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=(f"caretaker GitHub App is not installed on {actions.repository}"),
                )

        return actions

    return _dependency


__all__ = [
    "ActionsPrincipal",
    "GITHUB_ACTIONS_ISSUER",
    "configure_github_oidc",
    "is_configured",
    "principal_from_claims",
    "require_actions_principal",
    "reset",
    "set_installation_check",
]
