"""
Klavis Sandbox MCP Client for connecting to remote Klavis sandbox servers.
Uses StreamableHttp transport to connect to sandbox MCP servers acquired via Klavis API.
"""

import os
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from typing import Any
from .logger import create_logger

logger = create_logger(__name__)

# Configuration from environment
KLAVIS_API_KEY = os.getenv("KLAVIS_API_KEY", "")

DEFAULT_KLAVIS_MCP_SERVERS = [
    "weather"
]


class KlavisSandboxManager:
    KLAVIS_API_URL = "https://api.klavis.ai"

    def __init__(self):
        self.acquired_sandboxes: dict[str, dict] = {}  # server_name -> sandbox info
        self.sessions: dict[str, ClientSession] = {}  # server_name -> MCP session
        self._http_client: httpx.AsyncClient | None = None

    @property
    def http_client(self) -> httpx.AsyncClient:
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(
                headers={"Authorization": f"Bearer {KLAVIS_API_KEY}"},
                timeout=30.0,
            )
        return self._http_client

    async def acquire_sandbox(self, server_name: str) -> dict:
        """Acquire a sandbox for the given server."""
        url = f"{self.KLAVIS_API_URL}/sandbox/{server_name}"
        body = {"benchmark": "MCP_Atlas"} # with MCP_Atlas benchmark parameter, Klavis can initialize the environment from data_exports/README.md for you!

        logger.info(f"Acquiring Klavis sandbox for {server_name}...")
        response = await self.http_client.post(url, json=body if body else None)
        response.raise_for_status()

        data = response.json()
        self.acquired_sandboxes[server_name] = data
        logger.info(f"Acquired Klavis sandbox {data.get('sandbox_id')} for {server_name}")
        return data

    async def release_sandbox(self, server_name: str) -> None:
        """Release a sandbox for the given server."""
        sandbox = self.acquired_sandboxes.get(server_name)
        if not sandbox:
            return

        sandbox_id = sandbox.get("sandbox_id")
        url = f"{self.KLAVIS_API_URL}/sandbox/{server_name}/{sandbox_id}"

        try:
            logger.info(f"Releasing Klavis sandbox {sandbox_id} for {server_name}...")
            response = await self.http_client.delete(url)
            response.raise_for_status()
            logger.info(f"Released Klavis sandbox {sandbox_id}")
        except Exception as e:
            logger.error(f"Failed to release Klavis sandbox {sandbox_id}: {e}")
        finally:
            self.acquired_sandboxes.pop(server_name, None)

    async def acquire_all(self) -> None:
        """Acquire sandboxes for all configured servers."""
        servers = DEFAULT_KLAVIS_MCP_SERVERS.copy()
        logger.info(f"Acquiring {len(servers)} Klavis sandbox servers: {servers}")

        for server in servers:
            try:
                await self.acquire_sandbox(server)
            except Exception as e:
                logger.error(f"Failed to acquire Klavis sandbox for {server}: {e}")

    async def release_all(self) -> None:
        """Release all acquired sandboxes."""
        servers = list(self.acquired_sandboxes.keys())
        for server in servers:
            await self.release_sandbox(server)

        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None

    def get_server_url(self, server_name: str) -> str | None:
        """Get the MCP server URL for an acquired sandbox."""
        sandbox = self.acquired_sandboxes.get(server_name)
        if not sandbox:
            return None
        server_urls = sandbox.get("server_urls", {})
        return server_urls.get(server_name)

    def get_all_server_urls(self) -> dict[str, str]:
        """Get all acquired MCP server URLs."""
        urls = {}
        for server_name, sandbox in self.acquired_sandboxes.items():
            server_urls = sandbox.get("server_urls", {})
            if server_name in server_urls:
                urls[server_name] = server_urls[server_name]
        return urls


class KlavisSandboxMCPClient:
    """MCP Client that connects to Klavis sandbox servers via StreamableHttp transport."""

    def __init__(self, manager: KlavisSandboxManager):
        self.manager = manager
        self._sessions: dict[str, ClientSession] = {}
        self._exit_stacks: dict[str, Any] = {}

    async def __aenter__(self):
        """Connect to all acquired sandbox servers."""
        server_urls = self.manager.get_all_server_urls()
        for server_name, url in server_urls.items():
            try:
                await self._connect_server(server_name, url)
            except Exception as e:
                logger.error(f"Failed to connect to {server_name}: {e}")
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Disconnect from all servers."""
        for server_name in list(self._sessions.keys()):
            await self._disconnect_server(server_name)

    async def _connect_server(self, server_name: str, url: str) -> None:
        """Connect to a single Klavis sandbox MCP server via StreamableHttp."""
        from contextlib import AsyncExitStack

        logger.info(f"Connecting to {server_name} at {url}")
        exit_stack = AsyncExitStack()
        await exit_stack.__aenter__()

        # Create StreamableHttp connection
        read_stream, write_stream, _ = await exit_stack.enter_async_context(
            streamablehttp_client(url)
        )

        # Create and initialize session
        session = await exit_stack.enter_async_context(
            ClientSession(read_stream, write_stream)
        )
        await session.initialize()

        self._sessions[server_name] = session
        self._exit_stacks[server_name] = exit_stack
        logger.info(f"Connected to {server_name}")

    async def _disconnect_server(self, server_name: str) -> None:
        """Disconnect from a single server."""
        exit_stack = self._exit_stacks.pop(server_name, None)
        self._sessions.pop(server_name, None)
        if exit_stack:
            try:
                await exit_stack.__aexit__(None, None, None)
            except Exception as e:
                logger.error(f"Error disconnecting from {server_name}: {e}")

    async def list_tools(self) -> list:
        """List all tools from all connected servers."""
        all_tools = []
        for server_name, session in self._sessions.items():
            try:
                result = await session.list_tools()
                # Prefix tool names with server name
                for tool in result.tools:
                    tool.name = f"{server_name}_{tool.name}"
                    all_tools.append(tool)
            except Exception as e:
                logger.error(f"Failed to list tools from {server_name}: {e}")
        return all_tools

    async def call_tool(self, tool_name: str, arguments: dict) -> Any:
        """Call a tool on the appropriate server."""
        # Parse server name from tool name (format: servername_toolname)
        if "_" not in tool_name:
            raise ValueError(f"Invalid tool name format: {tool_name}")

        parts = tool_name.split("_", 1)
        server_name = parts[0]
        actual_tool_name = parts[1]

        session = self._sessions.get(server_name)
        if not session:
            raise ValueError(f"No connection to server: {server_name}")

        return await session.call_tool(actual_tool_name, arguments)


# Global manager instance
klavis_sandbox_manager = KlavisSandboxManager()
klavis_sandbox_client = KlavisSandboxMCPClient(klavis_sandbox_manager)
