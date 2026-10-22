/**
 * Configuration for the MCP evaluation server
 */

export const config = {
  // Server configuration
  port: process.env.PORT || 3001,
  // Bind address. Loopback by default: run_agent drives the operator's LLM key and
  // sandbox tools, so it must not be reachable from other hosts unless protected.
  host: process.env.HOST || '127.0.0.1',
  // Optional shared secret. When set, every request except /health must send
  // `Authorization: Bearer <HARNESS_AUTH_TOKEN>`. Required when HOST is not loopback.
  authToken: process.env.HARNESS_AUTH_TOKEN || '',

  // Server-side ceilings for caller-supplied loop limits.
  maxTurnsLimit: Number(process.env.MAX_TURNS_LIMIT) || 512,
  maxToolCallsLimit: Number(process.env.MAX_TOOL_CALLS_LIMIT) || 1000,

  // LLM proxy configuration (any LiteLLM-compatible endpoint)
  llmBaseUrl: process.env.LLM_BASE_URL || '',
  llmApiKeys: (process.env.LLM_API_KEY || '').split(',').map(k => k.trim()).filter(Boolean),
  get llmApiKey(): string {
    const keys = config.llmApiKeys;
    return keys[Math.floor(Math.random() * keys.length)];
  },

  // MCP sandbox URL (e.g. http://localhost:1984 when running agent-environment locally)
  mcpSandboxUrl: process.env.MCP_SANDBOX_URL || 'http://localhost:1984',

  // Logging configuration
  logLevel: process.env.LOG_LEVEL || 'info',

  // Request timeouts (ms). Raise these for slow / heavy-reasoning models so long
  // thinking calls and large tool payloads aren't cut off mid-run.
  toolCallTimeoutMs: Number(process.env.TOOL_CALL_TIMEOUT_MS) || 60_000,
  listToolsTimeoutMs: Number(process.env.LIST_TOOLS_TIMEOUT_MS) || 180_000,
  llmTimeoutMs: Number(process.env.LLM_TIMEOUT_MS) || 600_000,
}

// Validate required environment variables
if (config.llmApiKeys.length === 0) {
  throw new Error('LLM_API_KEY environment variable is required')
}
if (!config.llmBaseUrl) {
  throw new Error('LLM_BASE_URL environment variable is required')
}
if (!['127.0.0.1', 'localhost', '::1'].includes(config.host) && !config.authToken) {
  throw new Error(`HARNESS_AUTH_TOKEN is required when binding to a non-loopback HOST (${config.host})`)
}
