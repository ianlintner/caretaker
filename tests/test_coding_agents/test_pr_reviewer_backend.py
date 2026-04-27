"""PR reviewer routing now exposes a ``backend`` field for BYOCA selection."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from caretaker.config import PRReviewerConfig
from caretaker.pr_reviewer import handoff_reviewer
from caretaker.pr_reviewer.handoff_reviewer import (
    CLAUDE_CODE_REVIEW_MARKER,
    OPENCODE_REVIEW_MARKER,
)
from caretaker.pr_reviewer.routing import decide


def test_inline_decision_has_empty_backend() -> None:
    d = decide(
        additions=10,
        deletions=5,
        file_count=1,
        file_paths=["src/foo.py"],
        pr_labels=[],
        threshold=40,
        backend="opencode",
    )
    assert d.use_inline is True
    assert d.backend == ""


def test_handoff_decision_records_chosen_backend() -> None:
    # Big PR triggers hand-off; backend selection is preserved.
    d = decide(
        additions=900,
        deletions=200,
        file_count=30,
        file_paths=["src/auth/secret_key.py"],
        pr_labels=["architecture"],
        threshold=40,
        backend="opencode",
    )
    assert d.use_inline is False
    assert d.backend == "opencode"
    assert "opencode" in d.reason


def test_default_backend_is_claude_code() -> None:
    d = decide(
        additions=900,
        deletions=200,
        file_count=30,
        file_paths=[],
        pr_labels=[],
        threshold=40,
    )
    assert d.use_inline is False
    assert d.backend == "claude_code"


def test_known_backends_includes_both() -> None:
    backends = handoff_reviewer.known_backends()
    assert "claude_code" in backends
    assert "opencode" in backends


@pytest.mark.asyncio
async def test_dispatch_opencode_uses_distinct_marker() -> None:
    github = MagicMock()
    github.ensure_label = AsyncMock()
    github.add_labels = AsyncMock()
    github.upsert_issue_comment = AsyncMock()
    cfg = PRReviewerConfig()  # defaults
    ok = await handoff_reviewer.dispatch(
        backend="opencode",
        github=github,
        owner="o",
        repo="r",
        pr_number=1,
        config=cfg,
        routing_reason="test",
    )
    assert ok is True
    # Label string comes from PRReviewerConfig.opencode_label, not claude_code_label.
    github.add_labels.assert_awaited_once()
    label_args = github.add_labels.await_args.args
    assert label_args[3] == [cfg.opencode_label]
    # Marker passed to upsert is the opencode marker, not the claude one.
    upsert_kwargs = github.upsert_issue_comment.await_args.kwargs
    assert upsert_kwargs["marker"] == OPENCODE_REVIEW_MARKER
    assert OPENCODE_REVIEW_MARKER in upsert_kwargs["body"]
    assert CLAUDE_CODE_REVIEW_MARKER not in upsert_kwargs["body"]


@pytest.mark.asyncio
async def test_dispatch_unknown_backend_returns_false() -> None:
    github = MagicMock()
    cfg = PRReviewerConfig()
    ok = await handoff_reviewer.dispatch(
        backend="hermes",
        github=github,
        owner="o",
        repo="r",
        pr_number=1,
        config=cfg,
        routing_reason="test",
    )
    assert ok is False
