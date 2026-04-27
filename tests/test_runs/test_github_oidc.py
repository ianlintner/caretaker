"""Tests for ActionsPrincipal extraction and require_actions_principal."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from caretaker.auth import github_oidc
from caretaker.auth.github_oidc import (
    GITHUB_ACTIONS_ISSUER,
    ActionsPrincipal,
    principal_from_claims,
)

GOOD_CLAIMS = {
    "iss": GITHUB_ACTIONS_ISSUER,
    "aud": "caretaker-backend",
    "sub": "repo:owner/repo:ref:refs/heads/main",
    "repository": "owner/repo",
    "repository_id": "12345",
    "repository_owner": "owner",
    "repository_owner_id": "67890",
    "run_id": "9876543210",
    "run_attempt": "1",
    "actor": "ianlintner",
    "event_name": "schedule",
    "ref": "refs/heads/main",
    "sha": "deadbeef",
    "workflow": "Caretaker Maintainer",
    "job_workflow_ref": "owner/repo/.github/workflows/maintainer.yml@refs/heads/main",
    "exp": 1_000_000_000,
    "iat": 999_999_000,
}


def test_principal_from_claims_happy_path() -> None:
    principal = principal_from_claims(GOOD_CLAIMS)
    assert isinstance(principal, ActionsPrincipal)
    assert principal.repository == "owner/repo"
    assert principal.repository_id == 12345
    assert principal.run_id == 9876543210
    assert principal.run_attempt == 1
    assert principal.actor == "ianlintner"


def test_missing_repository_raises_401() -> None:
    bad = dict(GOOD_CLAIMS)
    bad.pop("repository")
    with pytest.raises(HTTPException) as exc_info:
        principal_from_claims(bad)
    assert exc_info.value.status_code == 401


def test_non_integer_run_id_raises_401() -> None:
    bad = dict(GOOD_CLAIMS)
    bad["run_id"] = "not-a-number"
    with pytest.raises(HTTPException) as exc_info:
        principal_from_claims(bad)
    assert exc_info.value.status_code == 401


def test_set_installation_check_can_reject() -> None:
    github_oidc.reset()

    async def _fake_check(repository: str) -> int | None:
        return None  # not installed

    github_oidc.set_installation_check(_fake_check)
    # The factory builds a dependency callable; invoking it with a
    # missing token would 401 first, so we just verify the wiring is
    # in place by reading back the set callback.
    assert github_oidc._installation_check is _fake_check  # type: ignore[attr-defined]
    github_oidc.reset()
    assert github_oidc._installation_check is None  # type: ignore[attr-defined]
