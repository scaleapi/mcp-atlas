/**
 * Handler entry point for MCP agent evaluation
 * Re-exports the main handler from agent-evals
 */

export { handleRunMCPAgentEval, handleCallMCPTool } from './agent-evals/agent-eval';
export { RunAgentAPIRequestBodySchema } from './schema';
