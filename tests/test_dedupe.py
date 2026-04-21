"""Tests for the shared PR dedupe helper."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from caretaker.dedupe import close_superseded_prs
from caretaker.github_client.models import Label, PRState, PullRequest, User


def _pr(number: int, *, title: str = "PR", body: str = "") -> PullRequest:
    now = datetime.now(UTC)
    return PullRequest(
        number=number,
        title=title,
        body=body,
        state=PRState.OPEN,
        user=User(login="Copilot", id=2, type="Bot"),
        head_ref=f"feature/{number}",
        base_ref="main",
        mergeable=True,
        merged=False,
        draft=False,
        labels=[Label(name="maintainer:internal")],
        created_at=now - timedelta(days=1),
        updated_at=now,
        html_url=f"https://github.com/o/r/pull/{number}",
    )


class TestCloseSupersededPrs:
    @pytest.mark.asyncio
    async def test_keeps_highest_numbered_closes_rest(self) -> None:
        prs = [_pr(10), _pr(15), _pr(22)]
        gh = AsyncMock()

        closed = await close_superseded_prs(
            gh,
            "o",
            "r",
            prs,
            bucket_key=lambda p: "bucket",
            comment=lambda c, k: f"Superseded by #{k.number}",
        )

        assert closed == [10, 15]
        # Keeper #22 never closed
        closed_calls = [
            c for c in gh.update_issue.await_args_list if c.kwargs.get("state") == "closed"
        ]
        closed_numbers = {c.args[2] for c in closed_calls}
        assert closed_numbers == {10, 15}
        # Each closed PR got a superseded comment
        commented_numbers = {c.args[2] for c in gh.add_issue_comment.await_args_list}
        assert commented_numbers == {10, 15}

    @pytest.mark.asyncio
    async def test_excludes_prs_with_none_key(self) -> None:
        prs = [
            _pr(10, title="upgrade v1.0"),
            _pr(11, title="unrelated"),
            _pr(12, title="upgrade v1.0"),
        ]
        gh = AsyncMock()

        def _key(p: PullRequest) -> str | None:
            return "upgrade" if "upgrade" in p.title else None

        closed = await close_superseded_prs(
            gh,
            "o",
            "r",
            prs,
            bucket_key=_key,
            comment=lambda c, k: "dup",
        )

        # Only #10 and #12 bucket together; keeps #12, closes #10
        assert closed == [10]

    @pytest.mark.asyncio
    async def test_single_pr_in_bucket_not_closed(self) -> None:
        prs = [_pr(10)]
        gh = AsyncMock()

        closed = await close_superseded_prs(
            gh,
            "o",
            "r",
            prs,
            bucket_key=lambda p: "solo",
            comment=lambda c, k: "",
        )

        assert closed == []
        gh.update_issue.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_buckets_are_independent(self) -> None:
        prs = [_pr(1, title="A"), _pr(2, title="A"), _pr(3, title="B"), _pr(4, title="B")]
        gh = AsyncMock()

        closed = await close_superseded_prs(
            gh,
            "o",
            "r",
            prs,
            bucket_key=lambda p: p.title,
            comment=lambda c, k: "dup",
        )

        # Each bucket keeps its own highest: A keeps 2, B keeps 4
        assert sorted(closed) == [1, 3]

    @pytest.mark.asyncio
    async def test_close_failure_does_not_abort_bucket(self) -> None:
        prs = [_pr(10), _pr(11), _pr(12)]
        gh = AsyncMock()

        async def _fail_first(owner: str, repo: str, number: int, **kwargs: object) -> None:
            if number == 10:
                raise RuntimeError("transient")

        gh.update_issue.side_effect = _fail_first

        closed = await close_superseded_prs(
            gh,
            "o",
            "r",
            prs,
            bucket_key=lambda p: "x",
            comment=lambda c, k: "dup",
        )

        # #10 close raised; #11 still closed
        assert closed == [11]
