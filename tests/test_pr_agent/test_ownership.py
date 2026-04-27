"""Tests for unified caretaker status comment (claim/readiness/release).

Verifies the Phase-F fix for PR #172: a single caretaker:status comment is
posted and edited in place across a PR's lifecycle instead of appending a new
claim/readiness/release comment on every evaluation.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from caretaker.config import OwnershipConfig
from caretaker.github_client.models import Comment, User
from caretaker.pr_agent.ownership import (
    STATUS_COMMENT_MARKER,
    build_status_comment,
    claim_ownership,
    compact_legacy_comments,
    find_status_comment,
    release_ownership,
    upsert_status_comment,
)
from caretaker.state.models import OwnershipState, TrackedPR
from tests.conftest import make_pr


def _comment(id_: int, body: str) -> Comment:
    return Comment(
        id=id_,
        user=User(login="github-actions[bot]", id=0, type="Bot"),
        body=body,
        created_at=datetime(2026, 4, 19, tzinfo=UTC),
    )


def _mock_github(existing: list[Comment] | None = None) -> AsyncMock:
    gh = AsyncMock()
    gh.get_pr_comments = AsyncMock(return_value=list(existing or []))
    gh.add_issue_comment = AsyncMock()
    gh.edit_issue_comment = AsyncMock()
    gh.delete_issue_comment = AsyncMock()
    gh.add_labels = AsyncMock()
    return gh


# ── find_status_comment ───────────────────────────────────────────────────────


class TestFindStatusComment:
    @pytest.mark.asyncio
    async def test_returns_none_when_no_marker(self) -> None:
        gh = _mock_github([_comment(1, "random human comment")])
        assert await find_status_comment(gh, "o", "r", 1) is None

    @pytest.mark.asyncio
    async def test_finds_by_new_marker(self) -> None:
        gh = _mock_github([_comment(7, f"{STATUS_COMMENT_MARKER}\nbody")])
        found = await find_status_comment(gh, "o", "r", 1)
        assert found is not None and found.id == 7

    @pytest.mark.asyncio
    async def test_finds_by_legacy_claim_marker(self) -> None:
        """Pre-migration PRs still have an old caretaker:ownership:claim comment."""
        gh = _mock_github([_comment(3, "<!-- caretaker:ownership:claim -->\n...")])
        found = await find_status_comment(gh, "o", "r", 1)
        assert found is not None and found.id == 3

    @pytest.mark.asyncio
    async def test_finds_by_legacy_readiness_marker(self) -> None:
        gh = _mock_github([_comment(4, "<!-- caretaker:readiness:update -->\n...")])
        found = await find_status_comment(gh, "o", "r", 1)
        assert found is not None and found.id == 4

    @pytest.mark.asyncio
    async def test_returns_most_recent_when_duplicates_exist(self) -> None:
        gh = _mock_github(
            [
                _comment(10, "<!-- caretaker:ownership:claim -->\nold"),
                _comment(15, "<!-- caretaker:ownership:claim -->\nnewer"),
                _comment(12, "<!-- caretaker:readiness:update -->\nmiddle"),
            ]
        )
        found = await find_status_comment(gh, "o", "r", 1)
        assert found is not None and found.id == 15


# ── upsert_status_comment ─────────────────────────────────────────────────────


class TestUpsertStatusComment:
    @pytest.mark.asyncio
    async def test_posts_new_when_none_exists(self) -> None:
        gh = _mock_github([])
        await upsert_status_comment(gh, "o", "r", 1, "body")
        gh.add_issue_comment.assert_awaited_once_with("o", "r", 1, "body")
        gh.edit_issue_comment.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_edits_when_body_differs(self) -> None:
        gh = _mock_github([_comment(42, f"{STATUS_COMMENT_MARKER}\nold")])
        await upsert_status_comment(gh, "o", "r", 1, f"{STATUS_COMMENT_MARKER}\nnew")
        gh.edit_issue_comment.assert_awaited_once()
        args = gh.edit_issue_comment.await_args.args
        assert args[2] == 42
        assert "new" in args[3]
        gh.add_issue_comment.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_noop_when_body_unchanged(self) -> None:
        body = f"{STATUS_COMMENT_MARKER}\nsame"
        gh = _mock_github([_comment(42, body)])
        await upsert_status_comment(gh, "o", "r", 1, body)
        gh.add_issue_comment.assert_not_awaited()
        gh.edit_issue_comment.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_migrates_legacy_claim_comment_in_place(self) -> None:
        """Legacy claim comment is edited, not duplicated by a new status comment."""
        gh = _mock_github([_comment(99, "<!-- caretaker:ownership:claim -->\nold")])
        await upsert_status_comment(gh, "o", "r", 1, f"{STATUS_COMMENT_MARKER}\nnew")
        gh.edit_issue_comment.assert_awaited_once()
        gh.add_issue_comment.assert_not_awaited()


# ── claim_ownership ───────────────────────────────────────────────────────────


def _copilot_pr(number: int = 100, draft: bool = False) -> object:
    return make_pr(
        number=number,
        user=User(login="copilot-swe-agent[bot]", id=1, type="Bot"),
        draft=draft,
    )


class TestClaimOwnershipIdempotent:
    @pytest.mark.asyncio
    async def test_first_claim_posts_new_comment(self) -> None:
        gh = _mock_github([])
        tracking = TrackedPR(number=100)
        result = await claim_ownership(gh, "o", "r", _copilot_pr(), tracking, OwnershipConfig())
        assert result.claimed is True
        gh.add_issue_comment.assert_awaited_once()
        gh.edit_issue_comment.assert_not_awaited()
        posted_body = gh.add_issue_comment.await_args.args[3]
        assert STATUS_COMMENT_MARKER in posted_body

    @pytest.mark.asyncio
    async def test_second_claim_edits_existing_no_duplicate(self) -> None:
        """If a second claim ever fires (stale state, concurrent worker),
        it must edit the existing status comment rather than append."""
        existing_body = f"{STATUS_COMMENT_MARKER}\nstub"
        gh = _mock_github([_comment(55, existing_body)])
        tracking = TrackedPR(number=100)
        await claim_ownership(gh, "o", "r", _copilot_pr(), tracking, OwnershipConfig())
        gh.add_issue_comment.assert_not_awaited()
        gh.edit_issue_comment.assert_awaited_once()
        assert gh.edit_issue_comment.await_args.args[2] == 55


# ── release_ownership flips comment body ──────────────────────────────────────


class TestReleaseFlipsStatusBody:
    @pytest.mark.asyncio
    async def test_release_edits_existing_status_comment(self) -> None:
        """Merge/close doesn't add a fourth comment — it edits the same one."""
        existing_body = f"{STATUS_COMMENT_MARKER}\nmonitoring"
        gh = _mock_github([_comment(88, existing_body)])
        tracking = TrackedPR(
            number=100,
            ownership_state=OwnershipState.OWNED,
            ownership_acquired_at=datetime.now(UTC),
        )
        pr = make_pr(number=100, merged=True, user=User(login="dev", id=3))
        result = await release_ownership(
            gh, "o", "r", pr, tracking, OwnershipConfig(), reason="PR merged"
        )
        assert result.released is True
        gh.edit_issue_comment.assert_awaited_once()
        assert gh.edit_issue_comment.await_args.args[2] == 88
        new_body = gh.edit_issue_comment.await_args.args[3]
        assert "🎉 Merged" in new_body


# ── build_status_comment transitions ──────────────────────────────────────────


class TestBuildStatusCommentTransitions:
    def test_monitoring_body_when_score_below_one(self) -> None:
        tracking = TrackedPR(
            number=1,
            readiness_score=0.2,
            readiness_blockers=["ci_pending"],
            ownership_state=OwnershipState.OWNED,
            ownership_acquired_at=datetime(2026, 4, 19, 12, 0, tzinfo=UTC),
        )
        body = build_status_comment(make_pr(number=1), tracking)
        assert STATUS_COMMENT_MARKER in body
        assert "⏳ Monitoring" in body
        assert "`ci_pending`" in body
        assert "20%" in body

    def test_ready_body_when_score_one(self) -> None:
        tracking = TrackedPR(
            number=1,
            readiness_score=1.0,
            readiness_blockers=[],
            ownership_state=OwnershipState.OWNED,
            ownership_acquired_at=datetime(2026, 4, 19, 12, 0, tzinfo=UTC),
        )
        body = build_status_comment(make_pr(number=1), tracking)
        assert "✅ Ready for merge" in body
        assert "None — PR is ready!" in body

    def test_merged_body_includes_duration_and_merge_emoji(self) -> None:
        tracking = TrackedPR(
            number=1,
            readiness_score=1.0,
            ownership_state=OwnershipState.RELEASED,
            ownership_acquired_at=datetime(2026, 4, 19, 12, 0, tzinfo=UTC),
            ownership_released_at=datetime(2026, 4, 19, 15, 30, tzinfo=UTC),
        )
        pr = make_pr(number=1, merged=True)
        body = build_status_comment(pr, tracking, release_reason="PR merged")
        assert "🎉 Merged" in body
        assert "Released:" in body
        assert "Duration:" in body


# ── compact_legacy_comments ───────────────────────────────────────────────────


class TestCompactLegacyComments:
    @pytest.mark.asyncio
    async def test_no_op_when_no_caretaker_comments(self) -> None:
        gh = _mock_github([_comment(1, "human comment")])
        removed = await compact_legacy_comments(gh, "o", "r", 1)
        assert removed == 0
        gh.delete_issue_comment.assert_not_awaited()
        gh.edit_issue_comment.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_op_when_only_one_caretaker_comment(self) -> None:
        gh = _mock_github([_comment(7, f"{STATUS_COMMENT_MARKER}\nbody")])
        removed = await compact_legacy_comments(gh, "o", "r", 1)
        assert removed == 0
        gh.delete_issue_comment.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_collapses_many_legacy_keeping_newest(self) -> None:
        """11 ownership-claim + 10 readiness-update → keeps highest id, deletes 20."""
        comments: list[Comment] = []
        for i in range(11):
            comments.append(_comment(100 + i, "<!-- caretaker:ownership:claim -->\nclaim"))
        for i in range(10):
            comments.append(_comment(200 + i, "<!-- caretaker:readiness:update -->\nready"))
        gh = _mock_github(comments)

        removed = await compact_legacy_comments(gh, "o", "r", 1)

        assert removed == 20
        assert gh.delete_issue_comment.await_count == 20
        deleted_ids = {call.args[2] for call in gh.delete_issue_comment.await_args_list}
        # The newest (id 209) must NOT be deleted; everything else is.
        assert 209 not in deleted_ids
        assert len(deleted_ids) == 20

    @pytest.mark.asyncio
    async def test_keeps_newest_when_mix_of_status_and_legacy(self) -> None:
        gh = _mock_github(
            [
                _comment(1, "<!-- caretaker:ownership:claim -->\nold"),
                _comment(2, "<!-- caretaker:readiness:update -->\nold"),
                _comment(3, f"{STATUS_COMMENT_MARKER}\nnew"),
            ]
        )
        removed = await compact_legacy_comments(gh, "o", "r", 1)
        assert removed == 2
        deleted_ids = {call.args[2] for call in gh.delete_issue_comment.await_args_list}
        assert deleted_ids == {1, 2}

    @pytest.mark.asyncio
    async def test_falls_back_to_archive_edit_when_delete_fails(self) -> None:
        gh = _mock_github(
            [
                _comment(10, "<!-- caretaker:ownership:claim -->\na"),
                _comment(11, "<!-- caretaker:ownership:claim -->\nb"),
            ]
        )
        gh.delete_issue_comment.side_effect = Exception("403 forbidden")

        removed = await compact_legacy_comments(gh, "o", "r", 1)

        assert removed == 1
        gh.edit_issue_comment.assert_awaited_once()
        edited_args = gh.edit_issue_comment.await_args.args
        assert edited_args[2] == 10  # the older comment
        assert "archived" in edited_args[3].lower()

    @pytest.mark.asyncio
    async def test_returns_zero_when_all_deletes_and_edits_fail(self) -> None:
        gh = _mock_github(
            [
                _comment(10, "<!-- caretaker:ownership:claim -->\na"),
                _comment(11, "<!-- caretaker:ownership:claim -->\nb"),
            ]
        )
        gh.delete_issue_comment.side_effect = Exception("delete failed")
        gh.edit_issue_comment.side_effect = Exception("edit failed")
        removed = await compact_legacy_comments(gh, "o", "r", 1)
        assert removed == 0


class TestTrackedPRCompactionFlag:
    def test_default_is_false(self) -> None:
        assert TrackedPR(number=1).legacy_comments_compacted is False
