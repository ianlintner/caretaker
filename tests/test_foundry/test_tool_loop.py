"""Tests for the tool-use loop."""

from __future__ import annotations

from typing import Any

import pytest

from caretaker.foundry.tool_loop import ToolLoopError, run_tool_loop
from caretaker.foundry.tools import ToolContext, build_tool_registry
from caretaker.llm.provider import LLMRequest, LLMResponse, LLMToolCall, LLMToolResponse

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

    @pytest.mark.asyncio
    async def test_missing_raw_message_raises_tool_loop_error(self, tool_ctx: ToolContext) -> None:
        """Providers must populate ``raw_message`` on tool-use responses.

        The loop used to silently rebuild the assistant message in OpenAI
        function-calling shape, which 400s on the next turn for Anthropic
        tool_use. Ensure we now fail loudly with :class:`ToolLoopError`.
        """

        class RawMessageMissingProvider:
            name = "raw-missing"
            available = True

            async def complete(self, request: LLMRequest) -> LLMResponse:
                return LLMResponse(text="", model=request.model, provider=self.name)

            async def complete_with_tools(
                self, request: LLMRequest, tools: list[dict[str, Any]]
            ) -> LLMToolResponse:
                return LLMToolResponse(
                    text="",
                    tool_calls=[LLMToolCall(id="call_1", name="git_status", arguments={})],
                    model=request.model,
                    provider=self.name,
                    raw_message=None,
                )

        with pytest.raises(ToolLoopError, match="raw_message=None"):
            await run_tool_loop(
                provider=RawMessageMissingProvider(),
                system_prompt="sys",
                user_prompt="usr",
                tools=build_tool_registry(),
                tool_ctx=tool_ctx,
                model="azure_ai/fake",
                max_iterations=3,
            )

    @pytest.mark.asyncio
    async def test_anthropic_shape_raw_message_is_round_tripped(
        self, tool_ctx: ToolContext
    ) -> None:
        """Anthropic-style ``tool_use`` content blocks must flow through unchanged.

        Previously the loop would have papered over a None ``raw_message`` by
        rebuilding the assistant turn in OpenAI shape, which Anthropic rejects.
        The loop now appends whatever the provider returns verbatim; this test
        asserts we round-trip the native Anthropic shape into the next request.
        """

        anthropic_assistant_message: dict[str, Any] = {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "git_status",
                    "input": {},
                },
            ],
        }

        class AnthropicShapeProvider:
            name = "anthropic-shape"
            available = True

            def __init__(self) -> None:
                # Snapshots of ``messages`` at each call, since the loop mutates
                # the same list in place between turns.
                self.message_snapshots: list[list[dict[str, Any]]] = []
                self._turn = 0

            async def complete(self, request: LLMRequest) -> LLMResponse:
                return LLMResponse(text="", model=request.model, provider=self.name)

            async def complete_with_tools(
                self, request: LLMRequest, tools: list[dict[str, Any]]
            ) -> LLMToolResponse:
                assert request.messages is not None
                self.message_snapshots.append([dict(m) for m in request.messages])
                self._turn += 1
                if self._turn == 1:
                    return LLMToolResponse(
                        text="",
                        tool_calls=[LLMToolCall(id="toolu_1", name="git_status", arguments={})],
                        model=request.model,
                        provider=self.name,
                        raw_message=anthropic_assistant_message,
                    )
                return LLMToolResponse(
                    text="done",
                    tool_calls=[],
                    model=request.model,
                    provider=self.name,
                    raw_message={"role": "assistant", "content": "done"},
                )

        provider = AnthropicShapeProvider()
        result = await run_tool_loop(
            provider=provider,
            system_prompt="sys",
            user_prompt="usr",
            tools=build_tool_registry(),
            tool_ctx=tool_ctx,
            model="claude-sonnet-4",
            max_iterations=5,
        )
        assert result.stopped_reason == "completed"
        assert result.iterations == 2
        assert result.tool_calls_made == 1
        # Second request must have the verbatim Anthropic-shape assistant turn
        # appended — not an OpenAI-rebuilt ``tool_calls`` list.
        second_messages = provider.message_snapshots[1]
        assistant_turns = [m for m in second_messages if m.get("role") == "assistant"]
        assert assistant_turns == [anthropic_assistant_message]

    @pytest.mark.asyncio
    async def test_openai_shape_raw_message_is_round_tripped(self, tool_ctx: ToolContext) -> None:
        """OpenAI function-calling shape round-trips too (LiteLLM happy path).

        This is the shape the FakeToolProvider in conftest emits; the other
        tests already cover it implicitly, but assert it explicitly so the
        provider-specific behaviour doesn't silently regress.
        """
        provider = FakeToolProvider(
            [
                ScriptedTurn(
                    tool_calls=[LLMToolCall(id="call_1", name="git_status", arguments={})]
                ),
                ScriptedTurn(text="done"),
            ]
        )
        result = await run_tool_loop(
            provider=provider,
            system_prompt="sys",
            user_prompt="usr",
            tools=build_tool_registry(),
            tool_ctx=tool_ctx,
            model="azure_ai/fake",
            max_iterations=5,
        )
        assert result.stopped_reason == "completed"
        # The loop mutates ``request.messages`` in place, so inspect whatever
        # state the list is in at the end of the run — it should contain the
        # first assistant turn carrying OpenAI-shape ``tool_calls``.
        second_messages = provider.calls[1].messages
        assert second_messages is not None
        assistant_turns_with_calls = [
            m for m in second_messages if m.get("role") == "assistant" and m.get("tool_calls")
        ]
        assert len(assistant_turns_with_calls) == 1
        tool_calls = assistant_turns_with_calls[0]["tool_calls"]
        assert isinstance(tool_calls, list)
        assert tool_calls[0]["type"] == "function"
        assert tool_calls[0]["function"]["name"] == "git_status"
