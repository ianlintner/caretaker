"""Tests for the in-memory fallback path of RunsStore.

Production hits Redis Streams + Mongo; the in-memory implementation
covers the same interface so we can exercise sequence-dedup, history
replay, and the live-tail iterator without external dependencies.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from caretaker.runs.models import LogEntry, LogStream, RunRecord, RunStatus
from caretaker.runs.store import RunsStore, new_run_id


@pytest.fixture
def store() -> RunsStore:
    return RunsStore()  # no MONGODB_URL / REDIS_URL → in-memory backend


def _record(repo: str = "owner/r", gh_run_id: int = 1) -> RunRecord:
    return RunRecord(
        run_id=new_run_id(),
        repository=repo,
        repository_id=1,
        repository_owner=repo.split("/", 1)[0],
        gh_run_id=gh_run_id,
        gh_run_attempt=1,
    )


@pytest.mark.asyncio
async def test_create_run_idempotent(store: RunsStore) -> None:
    first = await store.create_run(_record())
    same_key = RunRecord(
        run_id=new_run_id(),
        repository=first.repository,
        repository_id=first.repository_id,
        repository_owner=first.repository_owner,
        gh_run_id=first.gh_run_id,
        gh_run_attempt=first.gh_run_attempt,
    )
    second = await store.create_run(same_key)
    assert second.run_id == first.run_id


@pytest.mark.asyncio
async def test_append_log_dedup_by_seq(store: RunsStore) -> None:
    record = await store.create_run(_record())
    e1 = LogEntry(seq=1, stream=LogStream.STDOUT, data="a")
    e2 = LogEntry(seq=2, stream=LogStream.STDOUT, data="b")
    e2_dup = LogEntry(seq=2, stream=LogStream.STDOUT, data="b again")

    assert await store.append_log(record.run_id, e1)
    assert await store.append_log(record.run_id, e2)
    assert not await store.append_log(record.run_id, e2_dup)

    history = await store.read_history(record.run_id, after_seq=0)
    assert [entry.seq for _id, entry in history] == [1, 2]


@pytest.mark.asyncio
async def test_history_filters_by_after_seq(store: RunsStore) -> None:
    record = await store.create_run(_record())
    for seq in (1, 2, 3, 4, 5):
        await store.append_log(
            record.run_id,
            LogEntry(seq=seq, stream=LogStream.STDOUT, data=str(seq)),
        )
    after = await store.read_history(record.run_id, after_seq=2)
    assert [e.seq for _id, e in after] == [3, 4, 5]


@pytest.mark.asyncio
async def test_tail_yields_new_entries(store: RunsStore) -> None:
    record = await store.create_run(_record())

    async def _producer() -> None:
        for seq in range(1, 4):
            await asyncio.sleep(0.01)
            await store.append_log(
                record.run_id,
                LogEntry(seq=seq, stream=LogStream.STDOUT, data=f"line-{seq}"),
            )

    received: list[int] = []

    async def _consumer() -> None:
        async for item in store.tail(record.run_id, last_stream_id="$", block_ms=200):
            if item is None:  # heartbeat tick
                if received and received[-1] >= 3:
                    return
                continue
            _sid, entry = item
            received.append(entry.seq)
            if entry.seq >= 3:
                return

    await asyncio.gather(_producer(), _consumer())
    assert received == [1, 2, 3]


@pytest.mark.asyncio
async def test_list_runs_filters(store: RunsStore) -> None:
    a = await store.create_run(_record(repo="a/x", gh_run_id=1))
    b = await store.create_run(_record(repo="b/y", gh_run_id=2))
    await store.update_run(b.run_id, status=RunStatus.SUCCEEDED)

    just_a = await store.list_runs(repository="a/x")
    assert {r.run_id for r in just_a} == {a.run_id}

    succeeded = await store.list_runs(status=RunStatus.SUCCEEDED)
    assert {r.run_id for r in succeeded} == {b.run_id}


@pytest.mark.asyncio
async def test_collection_creates_cosmos_safe_compound_indexes() -> None:
    """``_collection`` registers ``(status, started_at)`` and
    ``(repository, started_at)`` compounds so ``list_runs`` filter+sort
    queries don't trip Azure Cosmos DB's strict OrderBy contract.

    Regression for the production stalled-run sweeper, which calls
    ``list_runs(status=RUNNING).sort('started_at', -1)`` every 60s and
    failed under Cosmos when only the single-field ``idx_started_at``
    existed.
    """
    created: list[tuple[Any, dict[str, Any]]] = []

    fake_collection = MagicMock()

    async def _capture(spec: Any, **kwargs: Any) -> str:
        created.append((spec, kwargs))
        return kwargs.get("name", "idx")

    fake_collection.create_index = AsyncMock(side_effect=_capture)

    fake_db = MagicMock()
    fake_db.__getitem__.return_value = fake_collection

    fake_client = MagicMock()
    fake_client.__getitem__.return_value = fake_db

    rs = RunsStore(mongodb_url="mongodb://stub")
    rs._mongo_client = fake_client  # bypass connect

    col = await rs._collection()
    assert col is fake_collection

    names = {kwargs.get("name") for _spec, kwargs in created}
    # Pre-existing single-field/uniqueness indexes still present.
    assert "idx_run_id" in names
    assert "idx_natural_key" in names
    assert "idx_started_at" in names
    # New Cosmos-safe compounds.
    assert "idx_status_started_at" in names
    assert "idx_repository_started_at" in names

    # Verify the leading-prefix shape so the sort is actually covered.
    by_name = {kwargs["name"]: spec for spec, kwargs in created if "name" in kwargs}
    assert by_name["idx_status_started_at"][0][0] == "status"
    assert by_name["idx_status_started_at"][1][0] == "started_at"
    assert by_name["idx_repository_started_at"][0][0] == "repository"
    assert by_name["idx_repository_started_at"][1][0] == "started_at"
