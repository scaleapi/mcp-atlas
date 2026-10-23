"""Exercise the opt-in preset through the real config loader and HTTP MCP client."""

import asyncio
import importlib
import json
import re
import shutil
import sys
from pathlib import Path

import httpx
import pytest
from fastmcp.client.transports import StreamableHttpTransport


SOURCE = Path(__file__).parents[1] / "src" / "agent_environment"
ENDPOINT = "https://search.parallel.ai/mcp"
USER_AGENT = "MCP-Atlas (https://github.com/scaleapi/mcp-atlas)"


@pytest.fixture
def load_environment(tmp_path, monkeypatch):
    # Use the actual loader with an entrypoint-style generated config, without
    # overwriting the tracked placeholder or starting unrelated stdio servers.
    package = tmp_path / "agent_environment"
    package.mkdir()
    for name in ("__init__.py", "logger.py", "mcp_client.py", "mcp_server_template.json"):
        shutil.copyfile(SOURCE / name, package / name)
    template = (package / "mcp_server_template.json").read_text()
    variables = set(re.findall(r"\$\{([^}]+)\}", template))
    monkeypatch.syspath_prepend(str(tmp_path))
    previous = {
        name: module for name, module in sys.modules.copy().items()
        if name == "agent_environment" or name.startswith("agent_environment.")
    }

    def load(enabled="", **credentials):
        for name in variables | {"PARALLEL_API_KEY"}:
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("ENABLED_SERVERS", enabled)
        for name, value in credentials.items():
            monkeypatch.setenv(name, value)
        generated = re.sub(r"\$\{([^}]+)\}", lambda m: credentials.get(m[1], ""), template)
        (package / "mcp_server_config.json").write_text(generated)
        for name in list(sys.modules):
            if name == "agent_environment" or name.startswith("agent_environment."):
                sys.modules.pop(name)
        importlib.invalidate_caches()
        return importlib.import_module("agent_environment.mcp_client")

    yield load
    for name in list(sys.modules):
        if name == "agent_environment" or name.startswith("agent_environment."):
            sys.modules.pop(name)
    sys.modules.update(previous)


def test_parallel_is_optional_and_preserves_server_selection(load_environment):
    fresh = load_environment()
    assert set(fresh.config["mcpServers"]) == set(fresh.DEFAULT_SERVERS)
    assert len(fresh.DEFAULT_SERVERS) == 20
    assert "ddg-search" in fresh.config["mcpServers"]
    assert "parallel-search" not in fresh.config["mcpServers"]

    configured = load_environment(BRAVE_API_KEY="configured-brave", EXA_API_KEY="configured-exa")
    assert {"brave-search", "exa"} <= configured.config["mcpServers"].keys()
    assert "parallel-search" not in configured.config["mcpServers"]
    assert configured.config["mcpServers"]["brave-search"]["env"]["BRAVE_API_KEY"] == "configured-brave"

    explicit = load_environment("calculator,wikipedia", BRAVE_API_KEY="configured-brave")
    assert set(explicit.config["mcpServers"]) == {"calculator", "wikipedia"}
    ambient = load_environment(PARALLEL_API_KEY="unused-parallel")
    assert "parallel-search" not in ambient.config["mcpServers"]


@pytest.fixture
def mcp_http(monkeypatch):
    """Mock the HTTP boundary, retaining real MCP initialization and dispatch."""
    requests = []
    pending = asyncio.Event()

    async def respond(request):
        assert str(request.url) == ENDPOINT
        assert request.headers["user-agent"] == USER_AGENT
        assert "authorization" not in request.headers
        if request.method != "POST":
            return httpx.Response(405)
        body = json.loads(request.content)
        requests.append(body)
        method = body["method"]
        if "id" not in body:
            return httpx.Response(202)
        if method == "initialize":
            result = {
                "protocolVersion": body["params"]["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "parallel-fixture", "version": "1"},
            }
        elif method == "tools/list":
            result = {"tools": [
                {"name": name, "inputSchema": {"type": "object", "properties": {}}}
                for name in ("web_search", "web_fetch")
            ]}
        elif method == "tools/call":
            args = body["params"]["arguments"]
            if args.get("objective") == "cancel":
                pending.set()
                await asyncio.Event().wait()
            if args.get("objective") == "timeout":
                await asyncio.sleep(1)
            if args.get("objective") == "error":
                result = {"content": [{"type": "text", "text": "Search unavailable"}], "isError": True}
            else:
                result = {"content": [{"type": "text", "text": json.dumps({
                    "url": "https://docs.python.org/3/", "content": "Python documentation"
                })}], "isError": False}
        else:
            raise AssertionError(method)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})

    original_init = StreamableHttpTransport.__init__

    def init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        assert self.url == ENDPOINT
        self.httpx_client_factory = lambda **options: httpx.AsyncClient(
            transport=httpx.MockTransport(respond), **options
        )

    monkeypatch.setattr(StreamableHttpTransport, "__init__", init)
    return requests, pending


def test_anonymous_discovery_search_fetch_and_failures(load_environment, mcp_http):
    environment = load_environment("parallel-search")
    preset = environment.config["mcpServers"]["parallel-search"]
    assert preset == {"transport": "http", "url": ENDPOINT, "headers": {"User-Agent": USER_AGENT}}
    requests, pending = mcp_http

    async def exercise():
        async with environment.client as client:
            assert {t.name for t in await client.list_tools()} == {"web_search", "web_fetch"}
            search = await client.call_tool("web_search", {
                "objective": "Find Python docs", "search_queries": ["Python official documentation"]
            })
            assert "https://docs.python.org/3/" in search.content[0].text
            fetched = await client.call_tool("web_fetch", {"urls": ["https://docs.python.org/3/"]})
            assert "Python documentation" in fetched.content[0].text
            with pytest.raises(Exception, match="Search unavailable"):
                await client.call_tool("web_search", {"objective": "error"})
            with pytest.raises(Exception, match="[Tt]ime[do]? ?out|[Tt]imed out"):
                await client.call_tool("web_search", {"objective": "timeout"}, timeout=0.02)
            task = asyncio.create_task(client.call_tool("web_search", {"objective": "cancel"}))
            await asyncio.wait_for(pending.wait(), timeout=2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(exercise())
    assert {r["method"] for r in requests} >= {"initialize", "tools/list", "tools/call"}
    calls = [r["params"] for r in requests if r["method"] == "tools/call"]
    assert any(c["arguments"].get("urls") == ["https://docs.python.org/3/"] for c in calls)
