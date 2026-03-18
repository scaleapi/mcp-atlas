"""Sandbox MCP client implementation."""

import asyncio
import json
import logging
import os
from datetime import timedelta
from typing import Any, Dict, List, Optional

import httpx

from .base_client import MCPClient
from ..errors import MCPClientToolExecutionError
from ..schema import ToolDefinition, CallToolResponse, TextContent
from ..config import config
from ..user_tool import (
    USER_TOOL_ENABLED,
    ASK_USER_TOOL_DEFINITION,
    UserContext,
    simulate_user_response,
)

logger = logging.getLogger(__name__)

# Tool cache configuration
# NOTE: sgpml-cache is a Scale-internal package (hosted on AWS CodeArtifact).
# Caching is optional — set TOOL_CACHE_ENABLED=true only within Scale's infrastructure.
# Without sgpml-cache installed, the code falls back to non-cached tool calls.
TOOL_CACHE_ENABLED = os.getenv("TOOL_CACHE_ENABLED", "").lower() == "true"
TOOL_CACHE_REDIS_URL = os.getenv("TOOL_CACHE_REDIS_URL")
TOOL_CACHE_NAMESPACE = os.getenv("TOOL_CACHE_NAMESPACE", "mcp_eval")
TOOL_CACHE_TTL_DAYS = int(os.getenv("TOOL_CACHE_TTL_DAYS", "365"))

# Lazy singleton for datastore
_datastore: Optional[Any] = None


def _get_datastore():
    """Get or create the Redis datastore singleton (requires sgpml-cache)."""
    global _datastore
    if _datastore is None:
        from sgpml_cache.datastore import RedisDataStore
        _datastore = RedisDataStore(
            namespace=TOOL_CACHE_NAMESPACE,
            url=TOOL_CACHE_REDIS_URL,
        )
        logger.info(f"Initialized Redis cache: {TOOL_CACHE_NAMESPACE}@{TOOL_CACHE_REDIS_URL}")
    return _datastore


class SandboxMCPClient(MCPClient):
    """MCP client that connects to pre-running sandbox environments."""

    def __init__(
        self,
        sandbox_url: str,
        enabled_tools: Optional[List[str]] = None,  # if None, all tools are enabled
        user_context: Optional[UserContext] = None,
    ):
        self.sandbox_url = sandbox_url
        self.enabled_tools = enabled_tools
        self.user_context = user_context
        self.tool_call_timeout = config.TOOL_CALL_TIMEOUT
        self.list_tools_timeout = config.LIST_TOOLS_TIMEOUT

    async def list_tools(self) -> List[ToolDefinition]:
        """List available tools from the sandbox."""
        try:
            async with httpx.AsyncClient(timeout=self.list_tools_timeout) as client:
                response = await client.post(
                    f"{self.sandbox_url}/list-tools",
                    headers={"Content-Type": "application/json"},
                )
                response.raise_for_status()

                tools_data = response.json()
                tools = [ToolDefinition(**tool) for tool in tools_data]

                # Filter by enabled tools if specified
                if self.enabled_tools:
                    tools = [tool for tool in tools if tool.name in self.enabled_tools]

                # Add ask_user tool if enabled and user_context is provided
                if USER_TOOL_ENABLED and self.user_context is not None:
                    ask_user_tool = ToolDefinition(
                        name=ASK_USER_TOOL_DEFINITION["name"],
                        description=ASK_USER_TOOL_DEFINITION["description"],
                        inputSchema=ASK_USER_TOOL_DEFINITION["inputSchema"],
                    )
                    tools.append(ask_user_tool)
                    logger.info("Added ask_user tool to available tools")

                return tools

        except Exception as error:
            logger.error(f"Failed to list tools from sandbox: {error}")
            raise

    async def call_tool(self, tool_name: str, args: Any) -> CallToolResponse:
        """Call a tool in the sandbox."""
        # Intercept ask_user calls and handle locally (no caching needed)
        if tool_name == "ask_user" and USER_TOOL_ENABLED and self.user_context is not None:
            return await self._handle_ask_user(args)

        # Use caching if enabled
        if TOOL_CACHE_ENABLED:
            return await self._cached_tool_call(tool_name, args)
        else:
            return await self._make_sandbox_call(tool_name, args)

    async def _make_sandbox_call(self, tool_name: str, args: Any) -> CallToolResponse:
        """Make the actual HTTP call to sandbox (cacheable)."""
        try:
            body = {
                "tool_name": tool_name,
                "tool_args": args,
            }

            async with httpx.AsyncClient(timeout=self.tool_call_timeout) as client:
                response = await client.post(
                    f"{self.sandbox_url}/call-tool",
                    json=body,
                    headers={"Content-Type": "application/json"},
                )

                if response.status_code != 200:
                    error_text = response.text
                    return CallToolResponse(
                        content=[TextContent(type="text", text=error_text)],
                        is_error=True,
                    )

                response_data = response.json()
                return CallToolResponse(
                    content=response_data,
                    is_error=False,
                )

        except httpx.ReadTimeout:
            logger.error(f"Tool {tool_name} timed out after {self.tool_call_timeout}s")
            raise MCPClientToolExecutionError(
                f"Tool {tool_name} timed out after {self.tool_call_timeout}s"
            )
        except Exception as error:
            logger.error(f"Failed to call tool {tool_name} in sandbox: {error}")
            raise MCPClientToolExecutionError(
                f"Failed to call tool {tool_name}: {error}"
            )

    async def _cached_tool_call(self, tool_name: str, args: Any) -> CallToolResponse:
        """Tool call with caching (requires Scale-internal sgpml-cache package)."""
        try:
            from sgpml_cache import cache, CacheConfig
        except ImportError:
            logger.warning("sgpml-cache not installed, falling back to non-cached call")
            return await self._make_sandbox_call(tool_name, args)

        try:
            datastore = _get_datastore()
        except Exception as e:
            logger.warning(f"Failed to initialize cache datastore: {e}, falling back to non-cached call")
            return await self._make_sandbox_call(tool_name, args)

        # We need to create the cached function dynamically since we need access to self
        # and the cache decorator expects a standalone function
        @cache(CacheConfig(
            datastore=datastore,
            tool_name=f"mcp_{tool_name}",  # Prefix for clarity in Redis
            ttl=timedelta(days=TOOL_CACHE_TTL_DAYS),
            skip_mcp_tool_errors=True,  # Don't cache error responses
        ))
        async def _cached_call(args_json: str) -> dict:
            # Convert back from JSON for the actual call
            parsed_args = json.loads(args_json)
            response = await self._make_sandbox_call(tool_name, parsed_args)
            return response.model_dump()

        # Serialize args to JSON for deterministic cache key
        args_json = json.dumps(args, sort_keys=True)

        # Make cached call and reconstruct response
        result_dict = await _cached_call(args_json)
        return CallToolResponse(**result_dict)

    async def _handle_ask_user(self, args: Any) -> CallToolResponse:
        """Handle ask_user tool calls locally via LLM simulation."""
        try:
            question = args.get("question", "")
            if not question:
                return CallToolResponse(
                    content=[TextContent(type="text", text="Error: No question provided")],
                    is_error=True,
                )

            logger.info(f"Handling ask_user call: {question[:100]}...")

            # Simulate user response
            simulated_response = await simulate_user_response(
                question=question,
                context="",  # Could add conversation context here if needed
                user_context=self.user_context,
            )

            return CallToolResponse(
                content=[TextContent(type="text", text=simulated_response)],
                is_error=False,
            )

        except Exception as error:
            logger.error(f"Failed to handle ask_user: {error}")
            return CallToolResponse(
                content=[TextContent(type="text", text=f"Error simulating user response: {error}")],
                is_error=True,
            )

    @property
    def sandbox_info(self) -> Dict[str, Any]:
        """Get sandbox information."""
        return {
            "sandbox_url": self.sandbox_url,
        }
