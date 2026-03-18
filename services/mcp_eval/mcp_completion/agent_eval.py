"""MCP evaluation functionality."""

import json
import logging
from typing import AsyncGenerator, Dict, List, Union, Any, Optional

from .mcp_client import MCPClient, SandboxMCPClient
from .llm import create_completion, _transform_tool_calls
from .schema import (
    RunAgentAPIRequestBody,
    Message,
    AssistantMessage,
    ToolCallOutputMessage,
    TextContent,
    ImageContent,
    ResourceContent,
    Content,
    CallToolResponse,
    SystemMessage,
    UserMessage,
)
from .errors import MCPClientToolExecutionError
from .config import config
from .user_tool import UserContext

logger = logging.getLogger(__name__)


class AgentOutput:
    """MCP eval output wrapper."""

    def __init__(self, output_type: str, data: Any):
        self.type = output_type
        self.data = data


async def run_mcp_eval(
    mcp_client: MCPClient,
    model: str,
    messages: List[Message],
    max_turns: int,
    extra_body: Optional[Dict[str, Any]] = None,
) -> AsyncGenerator[AgentOutput, None]:
    """
    Simple MCP evaluation loop that keeps calling tools until the model decides there are no more tools to call.
    """
    tools = await mcp_client.list_tools()
    transformed_tools = _transform_tool_calls([tool.model_dump() for tool in tools])

    all_messages: List[Message] = list(messages)
    usage_totals = {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
    }

    for i in range(max_turns):
        assistant_message = None
        original_content = None

        try:
            # Use unified LiteLLM completion for all models
            result = await create_completion(
                model=model,
                messages=all_messages,
                tools=transformed_tools,
                extra_body=extra_body,
            )

            assistant_message = result.message
            original_content = result.original_content
            if result.usage:
                usage_totals["prompt_tokens"] += int(
                    result.usage.get("prompt_tokens", 0) or 0
                )
                usage_totals["completion_tokens"] += int(
                    result.usage.get("completion_tokens", 0) or 0
                )
                usage_totals["total_tokens"] += int(
                    result.usage.get(
                        "total_tokens",
                        (result.usage.get("prompt_tokens", 0) or 0)
                        + (result.usage.get("completion_tokens", 0) or 0),
                    )
                    or 0
                )

        except Exception as error:
            error_type = type(error).__name__
            error_msg = str(error)
            logger.error(f"Model create completion or parsing failed: {error_type}: {error_msg}")

            # Check for rate limit indicators
            if "rate" in error_msg.lower() or "quota" in error_msg.lower() or "429" in error_msg:
                logger.error(f"⚠️  RATE LIMIT ERROR in agent_eval: {error_msg}")

            # Re-raise as server error instead of graceful handling
            raise Exception(f"LLM completion failed: {error}")

        all_messages.append(assistant_message)

        yield AgentOutput("message", assistant_message.model_dump())

        tool_calls = assistant_message.tool_calls or []

        if tool_calls:
            for tool_call in tool_calls:
                try:
                    # Parse tool arguments
                    args = json.loads(tool_call.function["arguments"])

                    # Call the tool
                    response = await mcp_client.call_tool(
                        tool_call.function["name"],
                        args,
                    )

                    # Create tool call message
                    tool_call_message = ToolCallOutputMessage(
                        role="tool",
                        content=response.content,
                        tool_call_id=tool_call.id,
                    )

                    all_messages.append(tool_call_message)
                    yield AgentOutput("message", tool_call_message.model_dump())

                except Exception as error:
                    logger.error(
                        f"Tool call failed: {error}, tool: {tool_call.function['name']}"
                    )
                    # Re-raise tool execution errors as server errors
                    raise Exception(
                        f"Tool execution failed - tool: {tool_call.function['name']}, error: {error}"
                    )
        else:
            # No more tool calls, agent is done
            break

    if usage_totals["total_tokens"] > 0:
        yield AgentOutput("usage", usage_totals)


async def handle_run_mcp_eval(
    body: RunAgentAPIRequestBody,
) -> AsyncGenerator[AgentOutput, None]:
    """
    Shared handler for running MCP eval that can be used by different routers.

    Args:
        body: Request body matching RunAgentAPIRequestBodySchema format

            Yields:
        AgentOutput: Generator that yields either successful messages or errors during MCP eval execution
    """
    mcp_client = None

    # Build user_context if provided in request
    user_context = None
    if body.user_context:
        user_context = UserContext(
            original_prompt=body.user_context.original_prompt,
            removed_value=body.user_context.removed_value,
            underspecified_prompt=body.user_context.underspecified_prompt,
        )

    mcp_client = SandboxMCPClient(
        sandbox_url=config.MCP_SERVER_URL,
        enabled_tools=body.enabled_tools,
        user_context=user_context,
    )

    async for output in run_mcp_eval(
        mcp_client=mcp_client,
        model=body.model,
        messages=body.messages,
        max_turns=body.max_turns,
        extra_body=body.extra_body,
    ):
        yield output
