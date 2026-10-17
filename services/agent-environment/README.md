# Agent Environment

A Docker container with 36 benchmark Model Context Protocol (MCP) servers for AI agents,
plus an optional hosted connection for custom tasks.

## Quick Start

1. **Set up environment variables:**
   This depends on `.env` that should be passed in from the root level directory of this repo (copied from env.template).

   Setup the API keys for the MCP servers you want to use. You'll have to get your own API keys.
   
   For quick start, modify `.env` to set a few servers that don't need API keys:
   `ENABLED_SERVERS=calculator,wikipedia,filesystem,git,fetch`

2. **Run the container:**

   The preferred way is with Docker (run these commands from the root level directory)
   ```bash
   make build
   make run-docker
   ```

   The container takes 1-3 minutes to start up depending on the number of MCP servers enabled. Once ready, it provides HTTP POST endpoints for `/call-tool` and `/list-tools` on port 1984.

3. **Test it's working:**
   ```bash
   curl http://localhost:1984/health
   curl -X POST http://localhost:1984/list-tools
   ```

## Available MCP Servers

The template in `src/agent_environment/mcp_server_template.json` defines 36 benchmark
servers plus the optional Parallel Search MCP connection described below. Some
benchmark servers require API keys. To see required API keys and notes on how to
get them, see `env.template`.

- **No API keys needed:** calculator, wikipedia, filesystem, git, fetch, arxiv, weather, etc.
- **API keys required:** GitHub, Google Maps, Slack, Notion, Airtable, Twelve Data, and others

See `env.template` for basic information about each API key and where to get it. And see `data_exports/README.md` for info on how to upload data to online services.

## Server Selection

To run only specific servers (useful for testing without API keys):

```bash
# In your .env file:
ENABLED_SERVERS=calculator,wikipedia,filesystem,git,fetch
```

### Optional Parallel Search MCP

For custom web-research tasks, [Parallel Search MCP](https://docs.parallel.ai/integrations/mcp/search-mcp)
provides free web search and page fetching without an account or API key. Anonymous
search uses Fast mode. Enable its built-in Streamable HTTP connection explicitly:

```bash
# In your .env file, then rebuild and restart the sandbox:
ENABLED_SERVERS=parallel-search,fetch
```

Run `make build && make run-docker` from the repository root. `ENABLED_SERVERS`
selects exactly the listed servers, so include any other servers your task needs.
Leaving it empty keeps the existing default servers and API-key auto-detection;
Parallel is never enabled automatically.

With multiple servers enabled, the harness discovers `parallel-search_web_search`
and `parallel-search_web_fetch`. Include those names in your custom task's
`ENABLED_TOOLS` or harness request's `enabledTools`. `web_search` takes `objective`
and `search_queries`; `web_fetch` takes `urls`. If Parallel is the only enabled
server, FastMCP exposes the unprefixed names `web_search` and `web_fetch` instead.

This optional hosted endpoint is separate from the 36 version-pinned benchmark
servers. Its implementation and web results can change. Use custom tasks and
record the server/tool selection; results with this tool set are not equivalent
to the published MCP-Atlas benchmark. Anonymous service limits apply.

## API Keys

Check `env.template` for all available API key configurations.

## Implementation details
This depends on https://github.com/jlowin/fastmcp

By default, caching is enabled. If a request is successful, it will be cached, and subsequent identical requests will return the cached value.

At high throughputs, some of the MCP servers may not perform as well or may freeze up. Replicas are recommended for high throughput.
