/**
 * MCP Evaluation Server
 *
 * Minimal Express server that handles MCP agent evaluation requests.
 */

// Load environment variables (MUST be first import)
import './env'

import express from 'express'
import { config } from './config'
import { logger } from './logger'
import { handleRunMCPAgentEval } from './mcp-agent/handler'
import { RunAgentAPIRequestBodySchema } from './mcp-agent/schema'

const app = express()

// Middleware
app.use(express.json({ limit: '50mb' }))

// Health check endpoint
app.get('/health', (req, res) => {
  res.json({ status: 'ok', timestamp: new Date().toISOString() })
})

// Main evaluation endpoint
app.post('/v2/mcp_eval/run_agent', async (req, res) => {
  try {
    logger.info('Received run_agent request', {
      taskId: req.body.task_id,
      model: req.body.model,
    })

    // Validate request body
    const body = RunAgentAPIRequestBodySchema.parse(req.body)

    // Execute agent evaluation
    const agentExecutionGenerator = await handleRunMCPAgentEval(body)
    const results = []

    for await (const agentOutput of agentExecutionGenerator) {
      results.push(agentOutput)
    }

    logger.info('Agent execution completed', {
      taskId: body.task_id,
      outputCount: results.length,
    })

    res.json(results)
  } catch (error) {
    logger.error('Agent execution failed', {
      error: error instanceof Error ? error.message : String(error),
      stack: error instanceof Error ? error.stack : undefined,
    })

    res.status(500).json({
      error: 'Agent execution failed',
      message: error instanceof Error ? error.message : String(error),
    })
  }
})

// Start server
app.listen(config.port, () => {
  logger.info(`MCP Evaluation Server listening on port ${config.port}`)
  logger.info(`LLM Base URL: ${config.llmBaseUrl}`)
  logger.info(`MCP Sandbox URL: ${config.mcpSandboxUrl}`)
})
