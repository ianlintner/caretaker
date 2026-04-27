"""Zero-regression gate: when executor config is default, PRCopilotBridge
produces byte-identical output to the legacy implementation.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from caretaker.config import ExecutorConfig
from caretaker.llm.copilot import CopilotProtocol, CopilotTask, TaskType
from caretaker.pr_agent.ci_triage import FailureType, TriageResult
from caretaker.pr_agent.copilot import PRCopilotBridge

from .test_dispatcher import _make_comment, _make_pr


class TestZeroRegression:
    def test_default_executor_config_provider_is_copilot(self) -> None:
        cfg = ExecutorConfig()
        assert cfg.provider == "copilot"
        assert cfg.foundry.enabled is False

    @pytest.mark.asyncio
    async def test_bridge_without_dispatcher_uses_protocol(self) -> None:
        """When PRCopilotBridge is constructed without a dispatcher, it must
        route through ``protocol.post_task`` exactly like before.
        """
        protocol = MagicMock(spec=CopilotProtocol)
        protocol.post_task = AsyncMock(return_value=_make_comment())

        bridge = PRCopilotBridge(protocol, max_retries=2)
        triage = TriageResult(
            failure_type=FailureType.LINT_FAILURE,
            job_name="lint",
            error_summary="E501",
            instructions="fix",
            raw_output="E501",
        )
        result = await bridge.request_ci_fix(_make_pr(), triage, attempt=1)

        assert result.task_posted is True
        assert result.comment_id == 555
        assert result.route is None  # no dispatcher path was taken
        protocol.post_task.assert_awaited_once()

        # Verify the task emitted carries the same marker + @copilot mention.
        posted = protocol.post_task.await_args.args[1]
        assert isinstance(posted, CopilotTask)
        assert posted.task_type == TaskType.LINT_FAILURE
        assert "@copilot" in posted.to_comment()
        assert "<!-- caretaker:task -->" in posted.to_comment()
