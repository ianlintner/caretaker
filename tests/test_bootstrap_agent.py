"""Tests for BootstrapAgent and the set_repo_variable GitHub client helper.

Covers:

* ``set_repo_variable`` POSTs first, falls back to PATCH on 422.
* ``get_repo_variable`` returns None on 404.
* BootstrapAgent skips repos that already have the marker file.
* BootstrapAgent commits the expected files + opens a PR for fresh repos.
* BootstrapAgent extracts target repos from both ``installation`` and
  ``installation_repositories`` event payload shapes.
"""

from __future__ import annotations

import base64
from unittest.mock import AsyncMock, MagicMock

import pytest

from caretaker.bootstrap_agent.agent import BootstrapAgent
from caretaker.github_client.api import GitHubAPIError, GitHubClient

# ── set_repo_variable / get_repo_variable ─────────────────────────────


@pytest.mark.asyncio
async def test_set_repo_variable_creates_new() -> None:
    client = GitHubClient(token="t")
    client._post = AsyncMock(return_value={})  # type: ignore[method-assign]
    await client.set_repo_variable("acme", "demo", "FOO", "bar")
    client._post.assert_awaited_once_with(
        "/repos/acme/demo/actions/variables",
        json={"name": "FOO", "value": "bar"},
    )


@pytest.mark.asyncio
async def test_set_repo_variable_falls_back_to_patch_on_422() -> None:
    client = GitHubClient(token="t")
    client._post = AsyncMock(  # type: ignore[method-assign]
        side_effect=GitHubAPIError(422, "already exists")
    )
    client._request = AsyncMock(return_value={})  # type: ignore[method-assign]

    await client.set_repo_variable("acme", "demo", "FOO", "bar")
    client._request.assert_awaited_once()
    method, path = client._request.call_args.args[:2]
    assert method == "PATCH"
    assert path == "/repos/acme/demo/actions/variables/FOO"


@pytest.mark.asyncio
async def test_set_repo_variable_propagates_non_422() -> None:
    client = GitHubClient(token="t")
    client._post = AsyncMock(  # type: ignore[method-assign]
        side_effect=GitHubAPIError(500, "server error")
    )
    with pytest.raises(GitHubAPIError):
        await client.set_repo_variable("acme", "demo", "FOO", "bar")


@pytest.mark.asyncio
async def test_get_repo_variable_returns_none_on_404() -> None:
    client = GitHubClient(token="t")
    client._get = AsyncMock(side_effect=GitHubAPIError(404, "not found"))  # type: ignore[method-assign]
    assert await client.get_repo_variable("acme", "demo", "MISSING") is None


@pytest.mark.asyncio
async def test_get_repo_variable_returns_value() -> None:
    client = GitHubClient(token="t")
    client._get = AsyncMock(return_value={"name": "FOO", "value": "bar"})  # type: ignore[method-assign]
    assert await client.get_repo_variable("acme", "demo", "FOO") == "bar"


# ── BootstrapAgent payload-target extraction ─────────────────────────


def test_target_repos_from_installation_repositories_added() -> None:
    payload = {
        "action": "added",
        "repositories_added": [
            {"full_name": "acme/alpha"},
            {"full_name": "acme/beta"},
        ],
    }
    assert BootstrapAgent._target_repos(payload) == [("acme", "alpha"), ("acme", "beta")]


def test_target_repos_from_installation_created() -> None:
    payload = {
        "action": "created",
        "repositories": [
            {"full_name": "globex/gamma"},
        ],
    }
    assert BootstrapAgent._target_repos(payload) == [("globex", "gamma")]


def test_target_repos_handles_empty() -> None:
    assert BootstrapAgent._target_repos(None) == []
    assert BootstrapAgent._target_repos({}) == []


# ── BootstrapAgent run() — fresh repo path ────────────────────────────


def _b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


def _fake_github(
    *,
    marker_present: bool = False,
    copilot_instructions_existing: str | None = None,
) -> MagicMock:
    """Return a MagicMock that mimics the GitHubClient surface BootstrapAgent uses."""
    client = MagicMock()

    async def get_file_contents(owner: str, repo: str, path: str) -> dict | None:
        if path == ".github/maintainer/.version":
            if marker_present:
                return {"content": _b64("0.0.1\n")}
            return None
        if path == ".github/copilot-instructions.md":
            if copilot_instructions_existing is not None:
                return {
                    "content": _b64(copilot_instructions_existing),
                    "sha": "abcd",
                }
            return None
        return None

    client.get_file_contents = AsyncMock(side_effect=get_file_contents)

    repo_obj = MagicMock()
    repo_obj.default_branch = "main"
    client.get_repo = AsyncMock(return_value=repo_obj)
    client.get_default_branch_sha = AsyncMock(return_value="deadbeef")
    client.create_branch = AsyncMock(return_value=None)
    client.create_or_update_file = AsyncMock(return_value={})
    client.create_pull_request = AsyncMock(return_value={"number": 7})
    client.set_repo_variable = AsyncMock(return_value=None)
    return client


@pytest.mark.asyncio
async def test_bootstrap_skips_when_marker_already_present() -> None:
    client = _fake_github(marker_present=True)
    agent = BootstrapAgent(
        github=client, owner="acme", repo="demo", backend_url="https://b.example"
    )
    report = await agent.run(event_payload={"repositories_added": [{"full_name": "acme/demo"}]})
    assert report.prs_opened == []
    assert "acme/demo" in report.prs_skipped_existing
    # Branch + commit + PR should never be touched on a skip.
    client.create_branch.assert_not_awaited()
    client.create_or_update_file.assert_not_awaited()
    client.create_pull_request.assert_not_awaited()
    # Variables are still set (idempotent) so a re-run can repair config.
    client.set_repo_variable.assert_awaited()


@pytest.mark.asyncio
async def test_bootstrap_opens_pr_and_sets_variable_for_fresh_repo() -> None:
    client = _fake_github(marker_present=False)
    agent = BootstrapAgent(
        github=client,
        owner="acme",
        repo="demo",
        backend_url="https://backend.example.com",
    )
    report = await agent.run(event_payload={"repositories_added": [{"full_name": "acme/demo"}]})

    assert report.prs_opened == [("acme/demo", 7)]
    assert any(name.endswith("CARETAKER_BACKEND_URL") for name in report.variables_set)

    # Branch was created.
    client.create_branch.assert_awaited_once()
    args = client.create_branch.call_args.args
    assert args[0] == "acme"
    assert args[1] == "demo"
    assert args[2] == "caretaker/bootstrap"
    assert args[3] == "deadbeef"

    # Each scaffolded file was committed (version + workflow + config + 3 personas + copilot).
    paths_committed = [
        call.kwargs.get("path") or (call.args[2] if len(call.args) > 2 else None)
        for call in client.create_or_update_file.call_args_list
    ]
    assert ".github/maintainer/.version" in paths_committed
    assert ".github/workflows/maintainer.yml" in paths_committed
    assert ".github/maintainer/config.yml" in paths_committed
    assert ".github/agents/maintainer-pr.md" in paths_committed
    assert ".github/agents/maintainer-issue.md" in paths_committed
    assert ".github/agents/maintainer-upgrade.md" in paths_committed
    assert ".github/copilot-instructions.md" in paths_committed

    # PR was opened against the default branch.
    pr_kwargs = client.create_pull_request.call_args.kwargs
    assert pr_kwargs["title"] == "chore: setup caretaker"
    assert pr_kwargs["head"] == "caretaker/bootstrap"
    assert pr_kwargs["base"] == "main"

    # Repo variable was set.
    client.set_repo_variable.assert_any_await(
        "acme", "demo", "CARETAKER_BACKEND_URL", "https://backend.example.com"
    )


@pytest.mark.asyncio
async def test_bootstrap_appends_block_to_existing_copilot_instructions() -> None:
    existing = "# Project guidelines\n\nDo not commit secrets.\n"
    client = _fake_github(marker_present=False, copilot_instructions_existing=existing)
    agent = BootstrapAgent(
        github=client, owner="acme", repo="demo", backend_url="https://b.example"
    )
    await agent.run(event_payload={"repositories_added": [{"full_name": "acme/demo"}]})

    # Find the copilot-instructions write; verify it merged with the existing content.
    matching = [
        call
        for call in client.create_or_update_file.call_args_list
        if (call.kwargs.get("path") or call.args[2]) == ".github/copilot-instructions.md"
    ]
    assert len(matching) == 1
    new_content = matching[0].kwargs.get("content")
    assert "Do not commit secrets" in new_content
    assert "## Caretaker" in new_content


@pytest.mark.asyncio
async def test_bootstrap_skips_copilot_append_if_already_present() -> None:
    existing = "## Caretaker\n\nAlready set up.\n"
    client = _fake_github(marker_present=False, copilot_instructions_existing=existing)
    agent = BootstrapAgent(
        github=client, owner="acme", repo="demo", backend_url="https://b.example"
    )
    await agent.run(event_payload={"repositories_added": [{"full_name": "acme/demo"}]})

    matching = [
        call
        for call in client.create_or_update_file.call_args_list
        if (call.kwargs.get("path") or call.args[2]) == ".github/copilot-instructions.md"
    ]
    # The agent should NOT have rewritten the file when the marker block is already present.
    assert matching == []


@pytest.mark.asyncio
async def test_bootstrap_dry_run_writes_nothing() -> None:
    client = _fake_github(marker_present=False)
    agent = BootstrapAgent(
        github=client,
        owner="acme",
        repo="demo",
        backend_url="https://b.example",
        dry_run=True,
    )
    report = await agent.run(event_payload={"repositories_added": [{"full_name": "acme/demo"}]})
    assert report.prs_opened == []
    client.create_branch.assert_not_awaited()
    client.create_or_update_file.assert_not_awaited()
    client.create_pull_request.assert_not_awaited()
