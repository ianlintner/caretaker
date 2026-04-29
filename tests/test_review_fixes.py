"""Regression tests for code-review feedback on PR #621.

One test per fix so a future regression is easy to attribute:

* ``recent_heartbeats`` pushes the limit into MongoDB rather than
  loading every doc into memory.
* ``_ConfigCache`` collapses concurrent fetches for the same repo
  onto a single Contents API call (and bounds the per-key lock dict).
* ``_file_template`` raises :class:`TemplateNotFoundError` when a
  template is missing, rather than silently returning an empty string.
* ``build_event_bus`` returns the same singleton across calls so the
  self-heal failure path doesn't leak connection pools.
* ``InstallationsIndex.list_repos`` uses single-flight semantics so
  concurrent miss-callers share one ``/app/installations`` enumeration.
* ``create_pull_request`` swallows label-attach failures.
"""

from __future__ import annotations

import asyncio
import base64
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from caretaker.bootstrap_agent.agent import (
    TemplateNotFoundError,
    _file_template,
)
from caretaker.eventbus import (
    LocalEventBus,
    build_event_bus,
    reset_event_bus,
)
from caretaker.github_app.context_factory import (
    GitHubAppContextFactory,
    _ConfigCache,
    reset_config_cache,
)
from caretaker.github_app.installation_tokens import InstallationToken
from caretaker.github_app.installations_index import FleetRepo, InstallationsIndex
from caretaker.github_app.webhooks import ParsedWebhook
from caretaker.github_client.api import GitHubAPIError, GitHubClient


@pytest.fixture(autouse=True)
def _isolated() -> None:
    """Clear shared singletons so each test starts clean."""
    reset_config_cache()
    reset_event_bus()
    yield
    reset_config_cache()
    reset_event_bus()


# ── Fix #1: recent_heartbeats applies limit in Mongo, not Python ─────


@pytest.mark.asyncio
async def test_recent_heartbeats_pushes_limit_into_mongo() -> None:
    """The cursor must be sorted descending and limited at the DB layer."""
    from caretaker.fleet.mongo_store import MongoFleetRegistryStore

    sort_calls: list[tuple[str, int]] = []
    limit_calls: list[int] = []

    class _FakeCursor:
        def __init__(self, docs: list[dict[str, Any]]) -> None:
            self._docs = docs

        def sort(self, field: str, direction: int) -> _FakeCursor:
            sort_calls.append((field, direction))
            return self

        def limit(self, n: int) -> _FakeCursor:
            limit_calls.append(n)
            # Mimic MongoDB: sorted desc, take the most recent n.
            return _FakeCursor(self._docs[-n:][::-1])

        def __aiter__(self) -> _FakeCursor:
            self._iter = iter(self._docs)
            return self

        async def __anext__(self) -> dict[str, Any]:
            try:
                return next(self._iter)
            except StopIteration as exc:
                raise StopAsyncIteration from exc

    class _FakeCollection:
        def __init__(self, docs: list[dict[str, Any]]) -> None:
            self._docs = docs

        def find(self, query: dict[str, Any]) -> _FakeCursor:
            return _FakeCursor(self._docs)

    rows = [{"repo": "acme/demo", "_id": i, "n": i} for i in range(1, 1000)]
    fake_db = {"fleet_heartbeats": _FakeCollection(rows)}

    store = MongoFleetRegistryStore(mongodb_url="mongodb://stub")

    async def _fake_db_accessor() -> Any:
        return fake_db

    store._db = _fake_db_accessor  # type: ignore[method-assign]
    store._ensure_indexes = AsyncMock(return_value=None)  # type: ignore[method-assign]

    items = await store.recent_heartbeats("acme/demo", limit=5)
    assert len(items) == 5
    assert sort_calls == [("_id", -1)]
    assert limit_calls == [5]


# ── Fix #2: _ConfigCache stampede protection ─────────────────────────


@pytest.mark.asyncio
async def test_config_cache_collapses_concurrent_fetches() -> None:
    """Five concurrent webhooks for the same repo → exactly one API call."""
    cache = _ConfigCache(redis_url="")

    cfg_yaml = yaml.dump({"version": "v1"})
    encoded = base64.b64encode(cfg_yaml.encode()).decode()

    api_calls = 0
    fetch_started = asyncio.Event()
    fetch_release = asyncio.Event()

    fake_client = MagicMock()

    async def fake_get(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal api_calls
        api_calls += 1
        fetch_started.set()
        # Block so that all callers queue behind us.
        await fetch_release.wait()
        return {"content": encoded}

    fake_client.get_file_contents = AsyncMock(side_effect=fake_get)

    minter = MagicMock()
    minter.get_token = AsyncMock(
        return_value=InstallationToken(token="t", expires_at=9_999_999_999, installation_id=1)
    )

    factory = GitHubAppContextFactory(minter=minter, llm_router=MagicMock(), config_cache=cache)

    parsed_template = ParsedWebhook(
        event_type="pull_request",
        delivery_id="d-X",
        action="opened",
        installation_id=1,
        repository_full_name="acme/widget",
        payload={},
    )

    # Patch GitHubClient construction to return our fake.
    import caretaker.github_app.context_factory as cf

    cf.GitHubClient = lambda token: fake_client  # type: ignore[assignment, misc]

    # Spawn five callers that all race the same cache miss.
    tasks = [asyncio.create_task(factory.build(parsed_template)) for _ in range(5)]
    await fetch_started.wait()
    fetch_release.set()
    results = await asyncio.gather(*tasks)

    assert len(results) == 5
    # The single-flight guarantee: only one Contents API call total,
    # not one per concurrent webhook.
    assert api_calls == 1


@pytest.mark.asyncio
async def test_config_cache_key_lock_dict_is_bounded() -> None:
    """The per-key lock dict must not grow past lru_capacity."""
    cache = _ConfigCache(redis_url="", lru_capacity=4)
    for i in range(20):
        await cache.acquire_key_lock("acme", f"repo-{i}")
    assert len(cache._key_locks) <= 4


# ── Fix #3: _file_template raises on missing template ────────────────


def test_file_template_raises_on_unknown_path() -> None:
    with pytest.raises(TemplateNotFoundError):
        _file_template(".github/no-such-file.yml")


def test_file_template_raises_when_root_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """If every candidate root fails, raise — never return ``''``."""
    monkeypatch.setenv("CARETAKER_BOOTSTRAP_TEMPLATES_DIR", "/nonexistent/path")
    # Force the in-tree resolver to also miss by patching `parents`.
    import caretaker.bootstrap_agent.agent as ba

    monkeypatch.setattr(
        ba,
        "_candidate_template_roots",
        lambda: [ba.Path("/nonexistent/a"), ba.Path("/nonexistent/b")],
    )
    with pytest.raises(TemplateNotFoundError) as excinfo:
        _file_template(".github/maintainer/config.yml")
    msg = str(excinfo.value)
    assert "looked at" in msg
    assert "CARETAKER_BOOTSTRAP_TEMPLATES_DIR" in msg


# ── Fix #4: build_event_bus returns a singleton ──────────────────────


def test_build_event_bus_is_a_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REDIS_URL", raising=False)
    a = build_event_bus()
    b = build_event_bus()
    assert a is b


def test_set_event_bus_overrides_singleton() -> None:
    custom = LocalEventBus()
    from caretaker.eventbus import set_event_bus

    set_event_bus(custom)
    assert build_event_bus() is custom


# ── Fix #5: InstallationsIndex single-flight ─────────────────────────


@pytest.mark.asyncio
async def test_installations_index_single_flight_under_concurrent_misses() -> None:
    """Concurrent ``list_repos`` callers share one ``_fetch_all`` task."""
    fetch_calls = 0
    fetch_started = asyncio.Event()
    fetch_release = asyncio.Event()

    class _StubIndex(InstallationsIndex):
        async def _fetch_all(self) -> list[FleetRepo]:  # type: ignore[override]
            nonlocal fetch_calls
            fetch_calls += 1
            fetch_started.set()
            await fetch_release.wait()
            return [FleetRepo(owner="acme", repo="demo", installation_id=1)]

    idx = _StubIndex(signer=MagicMock(), token_minter=MagicMock())

    tasks = [asyncio.create_task(idx.list_repos()) for _ in range(8)]
    await fetch_started.wait()
    fetch_release.set()
    results = await asyncio.gather(*tasks)

    assert all(len(r) == 1 for r in results)
    # Single-flight: only one underlying API enumeration despite 8 concurrent calls.
    assert fetch_calls == 1


# ── Fix #7: best-effort label attach ─────────────────────────────────


@pytest.mark.asyncio
async def test_create_pull_request_swallows_label_failure() -> None:
    """A label-attach 422 must not propagate; the PR creation succeeds."""
    client = GitHubClient(token="t")

    pr_response = {"number": 42}
    label_failure = GitHubAPIError(404, "Label not found")

    async def fake_post(path: str, json: dict | None = None) -> Any:
        if path.endswith("/pulls"):
            return pr_response
        if path.endswith("/issues/42/labels"):
            raise label_failure
        return {}

    client._post = AsyncMock(side_effect=fake_post)  # type: ignore[method-assign]

    # Should NOT raise even though the label POST fails.
    data = await client.create_pull_request(
        owner="acme",
        repo="demo",
        title="t",
        body="b",
        head="caretaker/bootstrap",
        base="main",
        labels=["caretaker:bootstrap"],
    )
    assert data == pr_response


@pytest.mark.asyncio
async def test_create_pull_request_swallows_assignee_failure() -> None:
    """Assignee-attach failure must also not propagate."""
    client = GitHubClient(token="t")
    pr_response = {"number": 42}

    async def fake_post(path: str, json: dict | None = None) -> Any:
        if path.endswith("/pulls"):
            return pr_response
        if path.endswith("/issues/42/assignees"):
            raise GitHubAPIError(422, "invalid assignee")
        return {}

    client._post = AsyncMock(side_effect=fake_post)  # type: ignore[method-assign]

    data = await client.create_pull_request(
        owner="acme",
        repo="demo",
        title="t",
        body="b",
        head="b",
        base="main",
        assignees=["nobody"],
    )
    assert data == pr_response
