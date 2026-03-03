#!/usr/bin/env python3
"""
Health-check script for all MCP servers defined in mcp_server_template.json.
Makes one representative call per server and reports pass/fail.

Server list and API-key requirements are derived automatically from the
template file — no need to update this script when servers are added/removed.

Usage:
    uv run test_servers.py
    uv run test_servers.py --timeout 30
    uv run test_servers.py --concurrency 10
    uv run test_servers.py --server github          # test a single server
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import time
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

# ── Paths ────────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent
REPO_ROOT = SCRIPT_DIR.parents[1]
TEMPLATE_PATH = (
    REPO_ROOT / "services/agent-environment/src/agent_environment/mcp_server_template.json"
)
ENV_PATH = REPO_ROOT / ".env"
BASE_URL = "http://localhost:1984/call-tool"


# ── Parse .env ───────────────────────────────────────────────────────────────
def load_env_keys(env_path: Path) -> set[str]:
    """Return the set of variable names that are set (non-empty) in .env."""
    if not env_path.exists():
        return set()
    keys: set[str] = set()
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        if value.strip():
            keys.add(name.strip())
    return keys


# ── Load server list from template ───────────────────────────────────────────
def _extract_vars(server_cfg: dict) -> list[str]:
    """Return all ${VAR_NAME} references found in a server config."""
    return re.findall(r"\$\{([A-Z_]+)\}", json.dumps(server_cfg))


def _uses_api_key(server_cfg: dict) -> bool:
    return bool(_extract_vars(server_cfg))


def load_servers() -> tuple[dict[str, bool], dict[str, list[str]]]:
    """Return ({server: needs_key}, {server: [VAR_NAMES]}) from the template."""
    with open(TEMPLATE_PATH) as f:
        data = json.load(f)
    servers = data.get("mcpServers", {})
    needs_key = {name: _uses_api_key(cfg) for name, cfg in servers.items()}
    required_vars = {name: _extract_vars(cfg) for name, cfg in servers.items()}
    return needs_key, required_vars


def build_random_oxylabs_query() -> str:
    """Build a realistic random Google query to avoid cache collisions."""
    topics = [
        "best laptops for coding",
        "python web framework comparison",
        "electric car range comparison",
        "top hiking trails in california",
        "best noise cancelling headphones",
        "ai coding assistant benchmark",
    ]
    modifiers = [
        "latest review",
        "buying guide",
        "pros and cons",
        "comparison",
        "beginner friendly",
        "expert recommendations",
    ]
    nonce = random.choice([5, 10, 20, 25, 50, 100])
    return f"{random.choice(topics)} {random.choice(modifiers)} {nonce}"


# ── Hardcoded test calls ──────────────────────────────────────────────────────
# One simple, read-only call per server that exercises real functionality.
# Key: server name exactly as it appears in mcp_server_template.json
TEST_CALLS: dict[str, tuple[str, dict]] = {
    # No API key
    "arxiv": (
        "arxiv_search_papers",
        {"query": "machine learning", "max_results": 1},
    ),
    "calculator": (
        "calculator_calculate",
        {"expression": "2 + 2"},
    ),
    "cli-mcp-server": (
        "cli-mcp-server_run_command",
        {"command": "ls /data"},
    ),
    "clinicaltrialsgov-mcp-server": (
        "clinicaltrialsgov-mcp-server_clinicaltrials_list_studies",
        {"query": {"term": "diabetes"}, "pageSize": 1},
    ),
    "context7": (
        "context7_resolve-library-id",
        {"libraryName": "react"},
    ),
    "ddg-search": (
        "ddg-search_search",
        {"query": "python programming"},
    ),
    "desktop-commander": (
        "desktop-commander_list_directory",
        {"path": "/data"},
    ),
    "fetch": (
        "fetch_fetch",
        {"url": "https://httpbin.org/get"},
    ),
    "filesystem": (
        "filesystem_list_allowed_directories",
        {},
    ),
    "git": (
        "git_git_status",
        {"repo_path": "/data/repos/mcp-server-calculator"},
    ),
    "memory": (
        "memory_search_nodes",
        {"query": "test"},
    ),
    "met-museum": (
        "met-museum_get-museum-object",
        {"objectId": 32907},
    ),
    "mcp-code-executor": (
        "mcp-code-executor_execute_code",
        {"code": "print(1 + 1)"},
    ),
    "mcp-server-code-runner": (
        "mcp-server-code-runner_run-code",
        {"languageId": "python", "code": "print(1 + 1)"},
    ),
    "open-library": (
        "open-library_get_book_by_title",
        {"title": "Dune"},
    ),
    "osm-mcp-server": (
        "osm-mcp-server_geocode_address",
        {"address": "New York City"},
    ),
    "pubmed": (
        "pubmed_search_pubmed_key_words",
        {"key_words": "diabetes"},
    ),
    "weather": (
        "weather_find_weather_stations",
        {"location": "48.0993244, -123.4256985"},
    ),
    "whois": (
        "whois_whois_domain",
        {"domain": "example.com"},
    ),
    "wikipedia": (
        "wikipedia_search_wikipedia",
        {"query": "python", "limit": 1},
    ),

    # Needs API key
    "airtable": (
        "airtable_list_bases",
        {},
    ),
    "alchemy": (
        "alchemy_fetchTokenPriceBySymbol",
        {"symbols": ["ETH"]},
    ),
    "brave-search": (
        "brave-search_brave_web_search",
        {"query": "latest AI news"},
    ),
    "e2b-server": (
        "e2b-server_run_code",
        {"code": "print(1 + 1)"},
    ),
    "exa": (
        "exa_web_search_exa",
        {"query": "python programming"},
    ),
    "github": (
        "github_list_commits",
        {"owner": "torvalds", "repo": "subsurface"},
    ),
    "google-maps": (
        "google-maps_maps_geocode",
        {"address": "New York City"},
    ),
    "google-workspace": (
        "google-workspace_list_events",
        {"maxResults": 1},
    ),
    "lara-translate": (
        "lara-translate_translate",
        {"text": [{"text": "Hello world", "translatable": True}], "target": "fr", "source": "en"},
    ),
    "mongodb": (
        "mongodb_list-databases",
        {},
    ),
    "national-parks": (
        "national-parks_findParks",
        {"q": "Yellowstone", "stateCode": "WY"},
    ),
    "notion": (
        "notion_API-get-users",
        {},
    ),
    "oxylabs": (
        "oxylabs_google_search_scraper",
        {"query": "python"},
    ),
    "slack": (
        "slack_channels_list",
        {"channel_types": "public_channel"},
    ),
    "twelvedata": (
        "twelvedata_GetPrice",
        {"params": {"symbol": "AAPL"}},
    ),
    "weather-data": (
        "weather-data_weather_current",
        {"q": "London"},
    ),
}


# ── Result dataclass ──────────────────────────────────────────────────────────
@dataclass
class Result:
    server: str
    needs_key: bool
    tool: str
    ok: bool
    elapsed: float
    status_code: int = 0
    preview: str = ""
    error: str = ""
    missing_keys: list[str] = None  # env vars that were absent in .env

    def __post_init__(self):
        if self.missing_keys is None:
            self.missing_keys = []


# ── Per-request logic ─────────────────────────────────────────────────────────
async def run_test(
    client: httpx.AsyncClient,
    server: str,
    needs_key: bool,
    tool: str,
    arguments: dict[str, Any],
    timeout: float,
) -> Result:
    payload = {"tool_name": tool, "tool_args": arguments}
    t0 = time.monotonic()
    try:
        resp = await client.post(BASE_URL, json=payload, timeout=timeout)
        elapsed = time.monotonic() - t0
        body = resp.text
        ok = resp.status_code < 300

        # Detect tool-level errors: MCP tools return [{type:text, text:"Error: ..."}]
        if ok:
            try:
                data = resp.json()
                if isinstance(data, dict) and "error" in str(data).lower():
                    ok = False
                elif isinstance(data, list):
                    for item in data:
                        if isinstance(item, dict):
                            text = item.get("text", "")
                            if isinstance(text, str) and text.startswith("Error:"):
                                ok = False
                                break
            except Exception:
                pass

        preview = body.replace("\n", " ").strip()[:120]
        return Result(server, needs_key, tool, ok, elapsed,
                      status_code=resp.status_code, preview=preview)
    except httpx.TimeoutException:
        elapsed = time.monotonic() - t0
        return Result(server, needs_key, tool, False, elapsed,
                      error=f"Timed out after {timeout}s")
    except Exception as exc:
        elapsed = time.monotonic() - t0
        return Result(server, needs_key, tool, False, elapsed, error=str(exc))


# ── Main ──────────────────────────────────────────────────────────────────────
async def main(timeout: float, concurrency: int, only_server: str | None) -> None:
    servers, required_vars = load_servers()
    env_keys = load_env_keys(ENV_PATH)
    total = len(servers)
    test_calls = {name: (tool, dict(args)) for name, (tool, args) in TEST_CALLS.items()}
    if "oxylabs" in test_calls:
        tool_name, tool_args = test_calls["oxylabs"]
        tool_args["query"] = build_random_oxylabs_query()
        test_calls["oxylabs"] = (tool_name, tool_args)

    # Warn about any servers in the template that lack a test call
    no_test = [s for s in servers if s not in TEST_CALLS]
    if no_test:
        print(f"\n⚠️  No test call defined for: {', '.join(no_test)}")
        print("   Add entries to TEST_CALLS in this script to cover them.\n")

    # Build the list of tests to run
    tests = [
        (name, servers[name], *test_calls[name])
        for name in servers
        if name in test_calls and (only_server is None or name == only_server)
    ]

    sem = asyncio.Semaphore(concurrency)

    async def bounded(client: httpx.AsyncClient, *args: Any) -> Result:
        async with sem:
            return await run_test(client, *args)

    async with httpx.AsyncClient() as client:
        tasks = [bounded(client, *t, timeout) for t in tests]
        results: list[Result] = await asyncio.gather(*tasks)

    # Annotate failed results with any missing .env keys
    for r in results:
        if not r.ok:
            r.missing_keys = [
                v for v in required_vars.get(r.server, [])
                if v not in env_keys
            ]

    # ── Print results ─────────────────────────────────────────────────────────
    no_key = [r for r in results if not r.needs_key]
    with_key = [r for r in results if r.needs_key]

    def render_group(title: str, group: list[Result]) -> None:
        if not group:
            return
        print(f"\n{'━' * 72}")
        print(f"  {title}")
        print(f"{'━' * 72}")
        for r in sorted(group, key=lambda x: x.server):
            icon = "✅" if r.ok else "❌"
            timing = f"{r.elapsed:.1f}s"
            if r.ok:
                detail = r.preview[:58]
            elif r.missing_keys:
                detail = f"not set in .env: {', '.join(r.missing_keys)}"
            else:
                detail = (r.error or r.preview)[:58]
            print(f"  {icon}  {r.server:<30}  {timing:>6}  {detail}")

    render_group("No API key required", no_key)
    render_group("API key required", with_key)

    passed = sum(1 for r in results if r.ok)
    tested = len(results)
    failed = [r for r in results if not r.ok]

    print(f"\n{'━' * 72}")
    if only_server:
        print(f"  Result: {passed}/{tested} passed (filtered to '{only_server}')", end="")
    else:
        print(f"  Result: {passed}/{total} passed  ({tested} tested, {total - tested} no test defined)", end="")
    if failed:
        print(f"\n  Failed: {', '.join(r.server for r in failed)}")
    else:
        print("  🎉 All clear!")
    print(f"{'━' * 72}\n")

    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Test all MCP servers from the template")
    parser.add_argument("--timeout", type=float, default=30,
                        help="Per-request timeout in seconds (default: 30)")
    parser.add_argument("--concurrency", type=int, default=8,
                        help="Max parallel requests (default: 8)")
    parser.add_argument("--server", metavar="NAME",
                        help="Test only this server (e.g. --server github)")
    args = parser.parse_args()

    asyncio.run(main(args.timeout, args.concurrency, args.server))
