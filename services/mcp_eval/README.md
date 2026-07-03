# MCP Eval — utilities

`test_servers.py` health-checks every MCP server defined in
[`../agent-environment/src/agent_environment/mcp_server_template.json`](../agent-environment/src/agent_environment/mcp_server_template.json):
it makes one representative call per server, reports pass/fail, and flags any
API keys missing from `.env`. Run it after adding your keys to confirm the
environment is healthy before a full eval:

```bash
python test_servers.py
```

`run_all.sh` invokes this automatically as a pre-flight check before the batch run.
