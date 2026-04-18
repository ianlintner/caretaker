"""Contract tests — Foundry output comments must parse as Copilot results.

The PR state machine keys off the ``<!-- caretaker:result -->`` markers and
``RESULT:`` / ``COMMIT:`` lines produced today by Copilot.  The Foundry
executor emits the same format — these tests guarantee the contract stays
stable.
"""

from __future__ import annotations

from caretaker.foundry.executor import _build_result_comment
from caretaker.llm.copilot import CopilotResult, ResultStatus


class TestResultCommentFormat:
    def test_fixed_result_parses_back(self) -> None:
        body = _build_result_comment(
            status=ResultStatus.FIXED,
            commit_sha="abc123",
            files_changed=2,
            insertions=10,
            deletions=3,
            iterations=5,
            summary_text="applied ruff autofix",
        )
        parsed = CopilotResult.parse(body)
        assert parsed is not None
        assert parsed.status == ResultStatus.FIXED
        assert parsed.commit == "abc123"
        assert "10" in parsed.changes  # insertions count appears in CHANGES

    def test_blocked_result_parses_back(self) -> None:
        body = _build_result_comment(
            status=ResultStatus.BLOCKED,
            commit_sha=None,
            files_changed=0,
            insertions=0,
            deletions=0,
            iterations=3,
            summary_text="",
            blocker="could not repro locally",
        )
        parsed = CopilotResult.parse(body)
        assert parsed is not None
        assert parsed.status == ResultStatus.BLOCKED
        assert parsed.blocker == "could not repro locally"

    def test_carries_maintainer_result_marker(self) -> None:
        body = _build_result_comment(
            status=ResultStatus.FIXED,
            commit_sha="sha",
            files_changed=1,
            insertions=1,
            deletions=0,
            iterations=1,
            summary_text="",
        )
        # The marker is what Comment.is_maintainer_result looks for.
        assert "<!-- caretaker:result -->" in body
