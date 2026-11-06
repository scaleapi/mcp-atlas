# Agent Environment

A Docker container with 36 pre-configured Model Context Protocol (MCP) servers for AI agents.

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

   Every endpoint except `/` and `/health` requires `Authorization: Bearer <SANDBOX_AUTH_TOKEN>`, and the service won't start without `SANDBOX_AUTH_TOKEN` set. `make run-docker` adds a random one to `.env` if it's missing.

3. **Test it's working:**
   ```bash
   export $(grep -E '^SANDBOX_AUTH_TOKEN=' .env)
   curl http://localhost:1984/health
   curl -X POST -H "Authorization: Bearer $SANDBOX_AUTH_TOKEN" http://localhost:1984/list-tools
   ```

## Available MCP Servers

This project includes 36 MCP servers as configured in `src/agent_environment/mcp_server_template.json`. Some require API keys. To see required API keys and notes on how to get them, see `env.template`

- **No API keys needed:** calculator, wikipedia, filesystem, git, fetch, arxiv, weather, etc.
- **API keys required:** GitHub, Google Maps, Slack, Notion, Airtable, Twelve Data, and others

See `env.template` for basic information about each API key and where to get it. And see `data_exports/README.md` for info on how to upload data to online services.

## Server Selection

To run only specific servers (useful for testing without API keys):

```bash
# In your .env file:
ENABLED_SERVERS=calculator,wikipedia,filesystem,git,fetch
```

## API Keys

Check `env.template` for all available API key configurations.

## Implementation details

### Met Museum search migration

The provider retired `/public/collection/v1/search` on October 1, 2026. The
pinned `metmuseum-mcp@0.9.2` still uses that path, so its process loads
`src/agent_environment/met_search_compat.cjs` through a Met-only `NODE_OPTIONS`
entry. Rebuild the container to include the file and updated template. If
running the template outside the container, adjust the preload's absolute path.
Do not set this preload globally for other MCP servers.

Only the old search GET is translated to `/v1.1/search`; object and department
requests, tool names, schemas and text formatting remain unchanged. Results are
collected sequentially in 500-ID pages, under one 45-second deadline, returning
the full `{total, objectIDs}` result for queries with at most 10,000 matches.
Broader queries fail explicitly with instructions to narrow the query: the
provider does not expose results beyond its 10,000-result window. Failed pages,
inconsistent totals and duplicate IDs also fail rather than returning a partial
set as complete. There are no hidden request retries. The functional preflight
now exercises search instead of only object lookup, which remained available
during the retirement.

This is an availability repair, **not historical result equivalence**. The new
backend changes matching and ordering, and broad searches can no longer return
an unbounded set. Record this environment change when comparing evaluations.
See the [provider migration guide](https://metmuseum.github.io/#migrating-from-v1search).

Run offline regression tests with Node 20:

```bash
node --test services/agent-environment/tests/met_search_compat.test.cjs
```

An optional live check starts the installed pinned stdio server, compares its
tool schemas with and without the preload, and exercises multi-page searches,
empty results and the unaffected tools. It needs public network access, but no
API keys; it is deliberately not run by CI. From the repository root:

```bash
node services/agent-environment/tests/met_search_live.mjs \
  "$(npm root -g)/metmuseum-mcp"
```

Inside the built agent-environment container, use `tests/met_search_live.mjs`
instead. No `.env` needs to be passed to this test.

This depends on https://github.com/jlowin/fastmcp

By default, caching is enabled. If a request is successful, it will be cached, and subsequent identical requests will return the cached value.

At high throughputs, some of the MCP servers may not perform as well or may freeze up. Replicas are recommended for high throughput.
