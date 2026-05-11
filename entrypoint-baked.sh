#!/bin/bash

# Source baked-in .env so all API keys are available without --env-file at runtime
if [ -f /agent-environment/.env ]; then
  set -a
  source /agent-environment/.env
  set +a
fi

# Generate the actual config from template by substituting environment variables
envsubst < src/agent_environment/mcp_server_template.json > src/agent_environment/mcp_server_config.json

exec "$@"
