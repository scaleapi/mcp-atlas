"""
Regression guard for scaleapi/mcp-atlas#34.

The filesystem MCP server @2025.11.25 returns structured content for
directory_tree that fails MCP output validation (-32602). Fixed upstream in
modelcontextprotocol/servers#3110 (2025.12.18+).

Template is source of truth; install_mcp_packages.sh must stay in sync
(test_mcp_config_sync.py). This test additionally enforces a minimum pin.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

TEMPLATE = (
    Path(__file__).parent.parent
    / "src"
    / "agent_environment"
    / "mcp_server_template.json"
)

# CalVer components — reject pins before the upstream fix shipped.
MIN_FILESYSTEM_CALVER = (2025, 12, 18)


def _parse_calver(version: str) -> tuple[int, ...]:
    parts = version.split(".")
    return tuple(int(p) for p in parts)


def _filesystem_pin_from_template() -> str:
    with open(TEMPLATE) as f:
        data = json.load(f)
    args = data["mcpServers"]["filesystem"]["args"]
    for arg in args:
        if arg.startswith("@modelcontextprotocol/server-filesystem@"):
            return arg
    raise AssertionError("filesystem server pin not found in mcp_server_template.json")


def test_filesystem_pin_meets_minimum_for_directory_tree():
    pin = _filesystem_pin_from_template()
    match = re.search(r"@([\d.]+)$", pin)
    assert match, f"unexpected filesystem pin format: {pin}"
    calver = _parse_calver(match.group(1))
    assert calver >= MIN_FILESYSTEM_CALVER, (
        f"filesystem pin {pin} is below minimum {MIN_FILESYSTEM_CALVER} "
        "(directory_tree MCP validation — see #34)"
    )
