"""LLM completion functionality using LiteLLM."""

import json
import logging
from typing import Any, Dict, List, Optional

import httpx
import litellm
from pydantic import BaseModel

from .schema import Message, ToolCallSchema, AssistantMessage
from .config import config

logger = logging.getLogger(__name__)

# Configure LiteLLM - suppress verbose logging
litellm.set_verbose = False
logging.getLogger("LiteLLM").setLevel(logging.WARNING)


class LLMResponse(BaseModel):
    """Response from LLM completion."""

    message: AssistantMessage
    original_content: Optional[str] = None


def configure_litellm():
    litellm.api_base = config.LLM_BASE_URL  # could also be just openai url
    litellm.api_key = config.LLM_API_KEY


# Configure LiteLLM once at module level
configure_litellm()


def is_anthropic_model(model: str) -> bool:
    """Check if model is an Anthropic model that supports caching."""
    model_lower = model.lower()
    return "anthropic/" in model_lower or "claude" in model_lower


def add_anthropic_cache_control(
    messages: List[Dict[str, Any]],
    tools: Optional[List[Dict[str, Any]]]
) -> None:
    """
    Add Anthropic prompt caching markers to messages and tools in-place.

    Anthropic allows a maximum of 4 cache control blocks.

    Caching strategy:
    1. Tools: Mark last tool for caching (1 block)
    2. Messages: Mark the last N messages to stay under 4 total blocks
       - If we have tools, we can cache up to 3 messages
       - If no tools, we can cache up to 4 messages
    """
    # Determine how many message blocks we can cache
    max_message_blocks = 3 if tools else 4

    # Count messages with string content (these are the ones we'll cache)
    cacheable_messages = [msg for msg in messages if msg.get("content") and isinstance(msg.get("content"), str)]

    # Only cache the last N messages to stay under the limit
    num_to_cache = min(len(cacheable_messages), max_message_blocks)
    messages_to_cache = cacheable_messages[-num_to_cache:] if num_to_cache > 0 else []

    # Transform selected messages to cacheable format
    for msg in messages:
        content = msg.get("content")
        if content and isinstance(content, str) and msg in messages_to_cache:
            msg["content"] = [
                {
                    "type": "text",
                    "text": content,
                    "cache_control": {"type": "ephemeral"}
                }
            ]

    # Mark last tool for caching
    if tools and len(tools) > 0:
        last_tool = tools[-1]
        if "function" in last_tool:
            last_tool["function"]["cache_control"] = {"type": "ephemeral"}


def strip_all_additional_properties(schema: any) -> any:
    """Recursively remove all `additionalProperties` keys from the schema."""
    if isinstance(schema, dict):
        # Remove 'additionalProperties' if it exists
        schema.pop("additionalProperties", None)

        # Recurse into all values
        for key, value in schema.items():
            strip_all_additional_properties(value)

    elif isinstance(schema, list):
        for item in schema:
            strip_all_additional_properties(item)

    return schema


async def create_completion(
    model: str,
    messages: List[Message],
    tools: List[ToolCallSchema],
    extra_body: Optional[Dict[str, Any]] = None,
) -> LLMResponse:
    """Create a completion using LiteLLM."""

    # Convert our schema to LiteLLM form at
    if "gemini" in model.lower():
        litellm_messages = [
            (
                msg.model_dump()
                if not isinstance(msg, AssistantMessage)
                else msg.original_message.model_dump()
            )
            for msg in messages
        ]
        litellm_tools = [
            strip_all_additional_properties(tool.model_dump()) for tool in tools
        ]
    else:
        litellm_messages = [msg.model_dump() for msg in messages]
        litellm_tools = [tool.model_dump() for tool in tools]

    # Add Anthropic prompt caching if applicable
    if is_anthropic_model(model):
        add_anthropic_cache_control(litellm_messages, litellm_tools)
        logger.info(f"Added Anthropic cache control markers to {len(litellm_messages)} messages and {len(litellm_tools) if litellm_tools else 0} tools")

    # These specific models route through an internal proxy that expects the
    # "openai/" prefix in the model name. LiteLLM strips one "openai/" prefix
    # when a custom api_base is set, so we double-prepend it here so the proxy
    # receives the correct name (e.g. "openai/macaroni-alpha").
    _PROXY_PREFIX_MODELS = ("openai/macaroni-alpha", "openai/galapagos-alpha")
    if config.LLM_BASE_URL and model in _PROXY_PREFIX_MODELS:
        proxy_model = "openai/" + model
    else:
        proxy_model = model

    try:
        response = await litellm.acompletion(
            model=proxy_model,
            messages=litellm_messages,
            tools=litellm_tools,
            api_key=config.LLM_API_KEY,
            api_base=config.LLM_BASE_URL,
            timeout=config.DEFAULT_TIMEOUT,
            **({"extra_body": extra_body} if extra_body else {}),
        )

        # Convert response back to our format
        # Handle tool_calls conversion from OpenAI format to our format
        tool_calls = None
        if response.choices[0].message.tool_calls:
            tool_calls = []
            for tool_call in response.choices[0].message.tool_calls:
                tool_calls.append(
                    {
                        "id": tool_call.id,
                        "type": tool_call.type,
                        "function": {
                            "name": tool_call.function.name,
                            "arguments": tool_call.function.arguments,
                        },
                    }
                )

        assistant_message = AssistantMessage(
            role="assistant",
            content=response.choices[0].message.content,
            tool_calls=tool_calls,
            original_message=response.choices[0].message,
        )

        return LLMResponse(message=assistant_message)

    except Exception as error:
        error_type = type(error).__name__
        error_msg = str(error)

        # Log detailed error information
        logger.error(f"LiteLLM completion failed: {error_type}: {error_msg}")

        # Check for specific rate limit errors
        if "rate" in error_msg.lower() or "quota" in error_msg.lower() or "429" in error_msg:
            logger.error(f"⚠️  RATE LIMIT DETECTED: {error_msg}")

        # Log full exception details for debugging
        if hasattr(error, 'response'):
            logger.error(f"  Response: {error.response}")
        if hasattr(error, 'status_code'):
            logger.error(f"  Status Code: {error.status_code}")

        raise


def _transform_tool_calls(tools: List[Dict[str, Any]]) -> List[ToolCallSchema]:
    """Transform tool definitions to ToolCallSchema format."""
    return [
        ToolCallSchema(
            type="function",
            function={
                "name": tool["name"],
                "description": tool["description"],
                "parameters": tool.get("input_schema", {}),
                "strict": False,
            },
        )
        for tool in tools
    ]
