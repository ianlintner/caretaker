"""Tests for the tool-use loop."""

from __future__ import annotations

import pytest

from caretaker.foundry.tool_loop import ToolLoopError, run_tool_loop
from caretaker.foundry.tools import ToolContext, build_tool_registry
from caretaker.llm.provider import LLMToolCall

from .conftest import FakeToolProvider, ScriptedTurn


class TestToolLoop:
    @pytest.mark.asyncio
    async def test_terminates_on_final_text(self, tool_ctx: ToolContext) -> None:
        provider = FakeToolProvider([ScriptedTurn(text="all done")])
        result = await run_tool_loop(
            provider=provider,
            system_prompt="system",
            user_prompt="user",
            tools=build_tool_registry(),
            tool_ctx=tool_ctx,
            model="azure_ai/fake",
            max_iterations=5,
        )
        assert result.stopped_reason == "completed"
        assert result.final_text == "all done"
        assert result.iterations == 1
        assert result.tool_calls_made == 0

    @pytest.mark.asyncio
    async def test_dispatches_tool_call_then_completes(self, tool_ctx: ToolContext) -> None:
        provider = FakeToolProvider(
            [
                ScriptedTurn(
                    tool_calls=[
                        LLMToolCall(
                            id="call_1",
                            name="read_file",
                            arguments={"path": "README.md"},
                        )
                    ]
                ),
                ScriptedTurn(text="read complete"),
            ]
        )
        result = await run_tool_loop(
            provider=provider,
            system_prompt="system",
            user_prompt="user",
            tools=build_tool_registry(),
            tool_ctx=tool_ctx,
            model="azure_ai/fake",
            max_iterations=5,
        )
        assert result.stopped_reason == "completed"
        assert result.tool_calls_made == 1
        assert result.iterations == 2

    @pytest.mark.asyncio
    async def test_stops_at_max_iterations(self, tool_ctx: ToolContext) -> None:
        # Provider keeps asking for tool calls forever.
        provider = FakeToolProvider(
            [
                ScriptedTurn(
                    tool_calls=[
                        LLMToolCall(
                            id=f"call_{i}",
                            name="git_status",
                            arguments={},
                        )
                    ]
                )
                for i in range(10)
            ]
        )
        result = await run_tool_loop(
            provider=provider,
            system_prompt="system",
            user_prompt="user",
            tools=build_tool_registry(),
            tool_ctx=tool_ctx,
            model="azure_ai/fake",
            max_iterations=3,
        )
        assert result.stopped_reason == "max_iterations"
        assert result.iterations == 3
        assert result.tool_calls_made == 3

    @pytest.mark.asyncio
    async def test_handles_unknown_tool(self, tool_ctx: ToolContext) -> None:
        provider = FakeToolProvider(
            [
                ScriptedTurn(
                    tool_calls=[LLMToolCall(id="c1", name="does_not_exist", arguments={})]
                ),
                ScriptedTurn(text="giving up"),
            ]
        )
        result = await run_tool_loop(
            provider=provider,
            system_prompt="system",
            user_prompt="user",
            tools=build_tool_registry(),
            tool_ctx=tool_ctx,
            model="azure_ai/fake",
            max_iterations=5,
        )
        assert result.stopped_reason == "completed"
        assert any("unknown_tool" in e for e in result.errors)

    @pytest.mark.asyncio
    async def test_provider_error_bubbles_up_as_tool_loop_error(
        self, tool_ctx: ToolContext
    ) -> None:
        class BrokenProvider:
            name = "broken"
            available = True

            async def complete(self, request):  # noqa: ANN001
                raise NotImplementedError

            async def complete_with_tools(self, request, tools):  # noqa: ANN001
                raise NotImplementedError("not supported")

        with pytest.raises(ToolLoopError):
            await run_tool_loop(
                provider=BrokenProvider(),
                system_prompt="sys",
                user_prompt="usr",
                tools=build_tool_registry(),
                tool_ctx=tool_ctx,
                model="azure_ai/fake",
                max_iterations=3,
            )
