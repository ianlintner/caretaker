"""Tool-use loop that drives the LLM against a tool registry.

The loop calls ``provider.complete_with_tools()`` with a growing list of
messages.  Each tool call emitted by the model is dispatched to the registered
handler and the result is appended as a ``tool`` message before the next
iteration.  Termination conditions:

- Model returns text without any tool calls (final answer).
- ``max_iterations`` reached.
- Cumulative input+output tokens exceed ``token_budget``.
- An unrecoverable exception is raised by a tool (the loop bails out and
  surfaces the error via the result).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from caretaker.llm.provider import LLMRequest

if TYPE_CHECKING:
    from caretaker.foundry.tools import Tool, ToolContext
    from caretaker.llm.provider import LLMProvider

logger = logging.getLogger(__name__)


class ToolLoopError(Exception):
    """Raised when the tool loop cannot continue (e.g., provider failure)."""


@dataclass
class ToolLoopResult:
    """Outcome of :func:`run_tool_loop`."""

    final_text: str
    iterations: int
    input_tokens: int
    output_tokens: int
    cost_usd: float | None
    mutations: list[str]
    stopped_reason: str  # "completed" | "max_iterations" | "token_budget" | "tool_error"
    tool_calls_made: int = 0
    errors: list[str] = field(default_factory=list)


async def run_tool_loop(
    *,
    provider: LLMProvider,
    system_prompt: str,
    user_prompt: str,
    tools: dict[str, Tool],
    tool_ctx: ToolContext,
    model: str,
    max_iterations: int = 20,
    token_budget: int = 200_000,
    max_tokens_per_turn: int = 4000,
    temperature: float = 0.0,
) -> ToolLoopResult:
    """Drive a model→tools→model conversation until the model emits a final
    answer or a termination limit is hit.
    """
    tool_schemas = [t.schema() for t in tools.values()]

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    total_input = 0
    total_output = 0
    total_cost: float | None = 0.0
    tool_calls_made = 0
    errors: list[str] = []

    for iteration in range(1, max_iterations + 1):
        request = LLMRequest(
            feature="foundry.tool_loop",
            prompt="",  # superseded by messages
            model=model,
            max_tokens=max_tokens_per_turn,
            temperature=temperature,
            messages=messages,
        )
        try:
            response = await provider.complete_with_tools(request, tool_schemas)
        except NotImplementedError as exc:
            raise ToolLoopError(
                f"provider {provider.name} does not support tool-use: {exc}"
            ) from exc
        except Exception as exc:
            raise ToolLoopError(f"provider call failed: {exc}") from exc

        total_input += response.input_tokens
        total_output += response.output_tokens
        if response.cost_usd is not None:
            total_cost = (total_cost or 0.0) + response.cost_usd

        # Append the assistant turn verbatim. Providers that implement
        # ``complete_with_tools`` MUST populate ``raw_message`` with a
        # provider-native message dict (OpenAI function-calling shape for
        # LiteLLM/OpenAI-compatible backends, Anthropic tool_use content-block
        # shape for native Anthropic backends, etc.). Rebuilding the assistant
        # turn here was previously hard-coded to OpenAI shape and would produce
        # a 400 on the next request when the provider expected Anthropic
        # tool_use blocks, so we now refuse to paper over a provider bug and
        # surface the contract violation as a ToolLoopError instead.
        if response.raw_message is None:
            raise ToolLoopError(
                f"provider {provider.name} returned a tool-use response with "
                "raw_message=None; cannot safely round-trip the assistant turn "
                "(provider must populate raw_message with its native message shape)"
            )
        messages.append(response.raw_message)

        if not response.tool_calls:
            return ToolLoopResult(
                final_text=response.text,
                iterations=iteration,
                input_tokens=total_input,
                output_tokens=total_output,
                cost_usd=total_cost,
                mutations=list(tool_ctx.mutations),
                stopped_reason="completed",
                tool_calls_made=tool_calls_made,
                errors=errors,
            )

        for tc in response.tool_calls:
            tool_calls_made += 1
            tool = tools.get(tc.name)
            if tool is None:
                tool_result = f'<tool-output kind="error">unknown tool: {tc.name}</tool-output>'
                errors.append(f"unknown_tool:{tc.name}")
            else:
                try:
                    tool_result = await tool.handler(tool_ctx, **tc.arguments)
                except TypeError as exc:
                    tool_result = (
                        f'<tool-output kind="error">invalid arguments '
                        f"for {tc.name}: {exc}</tool-output>"
                    )
                    errors.append(f"invalid_args:{tc.name}")
                except Exception as exc:  # tool internal failure
                    logger.warning("Tool %s raised: %s", tc.name, exc)
                    tool_result = f'<tool-output kind="error">{tc.name} raised: {exc}</tool-output>'
                    errors.append(f"tool_raised:{tc.name}")
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": tool_result,
                }
            )

        if total_input + total_output >= token_budget:
            return ToolLoopResult(
                final_text=response.text,
                iterations=iteration,
                input_tokens=total_input,
                output_tokens=total_output,
                cost_usd=total_cost,
                mutations=list(tool_ctx.mutations),
                stopped_reason="token_budget",
                tool_calls_made=tool_calls_made,
                errors=errors,
            )

    return ToolLoopResult(
        final_text="",
        iterations=max_iterations,
        input_tokens=total_input,
        output_tokens=total_output,
        cost_usd=total_cost,
        mutations=list(tool_ctx.mutations),
        stopped_reason="max_iterations",
        tool_calls_made=tool_calls_made,
        errors=errors,
    )
