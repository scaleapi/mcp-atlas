"""MCP client roots exposed to filesystem-related MCP servers."""

from __future__ import annotations

import os

# Matches desktop-commander allowedDirectories and filesystem server paths in
# mcp_server_template.json.
DEFAULT_CLIENT_ROOTS = ["/data"]


def parse_client_roots(raw: str | None = None) -> list[str]:
    """Parse MCP_CLIENT_ROOTS (comma-separated paths) for the FastMCP client."""
    value = raw if raw is not None else os.getenv(
        "MCP_CLIENT_ROOTS", ",".join(DEFAULT_CLIENT_ROOTS)
    )
    roots = [path.strip() for path in value.split(",") if path.strip()]
    return roots if roots else list(DEFAULT_CLIENT_ROOTS)
