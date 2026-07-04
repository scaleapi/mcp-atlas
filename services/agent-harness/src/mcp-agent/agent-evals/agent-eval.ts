import type {
  ToolCallDefinition,
  ToolCall,
  Message,
  ToolDefinition,
} from '../types';
import {
  AssistantMessageSchema,
  CallToolResponseSchema,
  RunAgentAPIRequestBodySchema,
  CallToolAPIRequestBodySchema,
} from '../schema';
import { getAgentCompletionStrategy } from './completion-strategy';
import { MCPClient, createMCPClient } from '../helpers/mcp-client';
import { SandboxMCPClient } from '../helpers/mcp-client/sandbox-client';
import { logger } from '../../logger';
import { config } from '../../config';
import { z } from 'zod';
const DEFAULT_MAX_TURNS = 256;
const DEFAULT_MAX_TOOL_CALLS = 100;

type AgentOutput = { type: 'message'; data: Message } | { type: 'error'; data: any };

interface RunAgentAPIOptions {
  mcpClient: MCPClient;
  model: string;
  messages: Message[];
  maxTurns?: number;
  strategy?: string;
  llmBaseUrl?: string;
  extraLlmParams?: Record<string, any>;
  taskId?: string;
  contextWindowManagement?: 'compact';
  toolOutputCap?: number;
  maxToolCalls?: number;
}

/**
 * Cap tool result content to a maximum number of characters.
 * Returns the content array with text truncated if needed.
 */
function capToolContent(content: any[], cap: number): any[] {
  const fullText = content.map((c: any) => c.text || '').join('');
  if (fullText.length <= cap) return content;
  const truncatedText = fullText.slice(0, cap) + `\n\n[Tool output truncated to ${cap} chars. Original was ${fullText.length} chars.]`;
  return [{ type: 'text', text: truncatedText }];
}

const COMPACT_KEEP_FULL_TURNS = 2;
const COMPACT_TRUNCATE_THRESHOLD = 1500;

/**
 * Compact messages by truncating old tool results to reduce context size.
 * Keeps full tool results for the last 2 turns.
 * Older tool results longer than 1500 chars are truncated to the first 1500 chars.
 * Only called when contextWindowManagement === 'compact'.
 */
function compactMessages(messages: Message[], currentTurn: number): Message[] {
  if (currentTurn <= COMPACT_KEEP_FULL_TURNS) return messages;

  // Find turn boundaries: each assistant message with tool_calls starts a new turn
  const turnStarts: number[] = [];
  for (let i = 0; i < messages.length; i++) {
    const msg = messages[i] as any;
    if (msg.role === 'assistant' && msg.tool_calls?.length > 0) {
      turnStarts.push(i);
    }
  }

  // Determine which turns to truncate (all except the last COMPACT_KEEP_FULL_TURNS)
  const turnsToTruncate = turnStarts.length - COMPACT_KEEP_FULL_TURNS;
  if (turnsToTruncate <= 0) return messages;

  const truncateBeforeIdx = turnStarts[turnsToTruncate];

  return messages.map((msg, idx) => {
    if (idx >= truncateBeforeIdx) return msg;
    const m = msg as any;
    if (m.role === 'tool') {
      const contentStr = Array.isArray(m.content)
        ? m.content.map((c: any) => c.text || '').join('')
        : String(m.content || '');
      if (contentStr.length > COMPACT_TRUNCATE_THRESHOLD) {
        const truncatedText = contentStr.slice(0, COMPACT_TRUNCATE_THRESHOLD) + `\n\n[Tool call output too large, truncated to ${COMPACT_TRUNCATE_THRESHOLD} chars. Original was ${contentStr.length} chars.]`;
        const truncated = {
          ...m,
          content: Array.isArray(m.content)
            ? [{ type: 'text', text: truncatedText }]
            : truncatedText,
        };
        return truncated as Message;
      }
    }
    return msg;
  });
}

function* handleCompletionError(error: any, mcpClient: MCPClient): Generator<AgentOutput> {
  if (mcpClient instanceof SandboxMCPClient) {
    logger.error('Model Create completion or parsing failed', {
      message: error.message || String(error),
      stack: error.stack || null,
      info: mcpClient.sandboxInfo,
      responseData: error.response?.data || null,
      responseStatus: error.response?.status || null,
    });
  } else {
    logger.error('Model Create Completion or Parsing Failed', {
      message: error.message || String(error),
      stack: error.stack || null,
      responseData: error.response?.data || null,
      responseStatus: error.response?.status || null,
    });
  }

  yield {
    type: 'error' as const,
    data: {
      message: error.message || String(error),
      serverResponse: error.response?.data || null,
    },
  };
}

/**
 * Simply agent loop that keeps calling tools until the model decides there are no more tools to call.
 */
async function* runMcpAgent({
  model,
  messages,
  mcpClient,
  maxTurns = DEFAULT_MAX_TURNS,
  strategy,
  llmBaseUrl,
  extraLlmParams,
  taskId,
  contextWindowManagement,
  toolOutputCap,
  maxToolCalls = DEFAULT_MAX_TOOL_CALLS,
}: RunAgentAPIOptions): AsyncGenerator<AgentOutput, void, unknown> {
  // Log agent loop configuration
  const sandboxInfo = mcpClient instanceof SandboxMCPClient ? mcpClient.sandboxInfo : null;
  logger.info('=== STARTING AGENT LOOP ===', {
    taskId,
    model,
    strategy: strategy || 'default (litellm)',
    maxTurns,
    maxToolCalls,
    toolOutputCap,
    messageCount: messages.length,
    sandboxId: sandboxInfo?.sandboxId,
    sandboxTags: sandboxInfo?.sandboxTags,
  });

  const tools = await mcpClient.listTools();
  logger.info('Available tools loaded', { toolCount: tools.length });

  // Reset sandbox state disabled — the agent-environment image
  // does not implement /reset-state (returns 404), causing noisy errors.
  // Re-enable if the Docker image adds this endpoint.
  const transformedTools = _transformToolCalls(tools);
  const allMessages: Message[] = [...messages];

  // Track whether the loop exhausted maxTurns without a natural break.
  // Set to false in every explicit break path; remains true only if the
  // for-loop condition (i < maxTurns) is what ended the loop.
  let reachedMaxTurns = true;
  let totalToolCalls = 0;
  let reachedMaxToolCalls = false;

  for (let i = 0; i < maxTurns; i++) {
    // Check tool call limit before next LLM call
    if (maxToolCalls && totalToolCalls >= maxToolCalls) {
      reachedMaxTurns = false;
      reachedMaxToolCalls = true;
      break;
    }
    logger.info(`[${taskId}] Turn ${i + 1}/${maxTurns}`, { messageCount: allMessages.length, totalToolCalls });
    let assistantMessage;
    try {
      // Get the appropriate strategy based on model and strategy parameter
      const completionStrategy = getAgentCompletionStrategy(model, strategy, llmBaseUrl);

      // Apply context compaction if enabled — truncate old tool results to save context space
      let messagesToSend = allMessages;
      if (contextWindowManagement === 'compact') {
        messagesToSend = compactMessages(allMessages, i);
        if (messagesToSend !== allMessages) {
          const originalChars = allMessages.reduce((sum, m) => sum + JSON.stringify(m).length, 0);
          const compactedChars = messagesToSend.reduce((sum, m) => sum + JSON.stringify(m).length, 0);
          const saved = originalChars - compactedChars;
          if (saved > 0) {
            logger.info(`[${taskId}] Compact: ${originalChars} → ${compactedChars} chars (saved ${saved}, ${(saved/originalChars*100).toFixed(1)}%)`);
            yield { type: 'compaction' as any, data: { turn: i + 1, originalChars, compactedChars, savedChars: saved, savedPct: Math.round(saved / originalChars * 100) } };
          }
        }
      }

      // Retry on transient errors (503, 429, network errors) up to 3 times
      let result;
      const MAX_LLM_RETRIES = 3;
      for (let retry = 0; retry < MAX_LLM_RETRIES; retry++) {
        try {
          result = await completionStrategy.createCompletion({
            model,
            messages: messagesToSend,
            tools: transformedTools,
            extraLlmParams,
          });
          break;
        } catch (retryError: any) {
          const status = retryError?.response?.status || retryError?.status;
          const isTimeout = retryError?.code === 'ECONNABORTED' || retryError?.message?.includes('timeout');
          const isRetryable = status === 500 || status === 502 || status === 503 || status === 429 || isTimeout;
          if (isRetryable && retry < MAX_LLM_RETRIES - 1) {
            const waitSec = isTimeout ? 15 : (status === 429 ? Math.min(2 ** retry * 5, 30) : 10);
            const logMsg = isTimeout
              ? `LLM call timed out, retrying in ${waitSec}s (attempt ${retry + 1}/${MAX_LLM_RETRIES})`
              : `LLM call failed with ${status}, retrying in ${waitSec}s (attempt ${retry + 1}/${MAX_LLM_RETRIES})`;
            logger.warn(`[${taskId}] ${logMsg}`);
            yield { type: 'log' as any, data: { level: 'warn', message: logMsg } };
            await new Promise(resolve => setTimeout(resolve, waitSec * 1000));
            continue;
          }
          throw retryError;
        }
      }

      const { message } = result!;

      assistantMessage = AssistantMessageSchema.parse(message);
    } catch (error) {
      // LLM completion or parsing failed, break the loop
      reachedMaxTurns = false;
      yield* handleCompletionError(error, mcpClient);
      break;
    }

    allMessages.push(assistantMessage);
    yield { type: 'message', data: assistantMessage };

    const toolCalls = assistantMessage.tool_calls ?? [];

    if (toolCalls.length > 0) {
      for (const rawToolCall of toolCalls) {
        // Check tool call limit before executing
        if (maxToolCalls && totalToolCalls >= maxToolCalls) {
          reachedMaxTurns = false;
          reachedMaxToolCalls = true;
          break;
        }
        totalToolCalls++;
        const toolCall = prunedTools(rawToolCall);
        try {
          const response = await mcpClient.callTool(
            toolCall.function.name,
            JSON.parse(toolCall.function.arguments),
          );
          const toolCallResult = CallToolResponseSchema.parse(response);
          const cappedContent = toolOutputCap
            ? capToolContent(toolCallResult.content, toolOutputCap)
            : toolCallResult.content;
          const toolCallMessage = {
            role: 'tool' as const,
            content: cappedContent,
            tool_call_id: toolCall.id,
          };
          allMessages.push(toolCallMessage);
          yield { type: 'message', data: toolCallMessage };
        } catch (error) {
          // Tool call failed — feed error back to model so it can recover
          const errorMsg = ((error as any).message || String(error)).split('\n')[0];
          logger.error(`[${taskId}] Tool call failed, feeding error back to model`, {
            toolCall: toolCall.function.name,
            error: errorMsg,
          });
          yield { type: 'log' as any, data: { level: 'error', message: `Tool ${toolCall.function.name} failed: ${errorMsg}` } };
          const errorToolMessage = {
            role: 'tool' as const,
            content: [{ type: 'text' as const, text: `Error: ${errorMsg}` }],
            tool_call_id: toolCall.id,
          };
          allMessages.push(errorToolMessage);
          yield { type: 'message', data: errorToolMessage };
        }
      }
      // Break outer loop if tool call limit reached mid-turn
      if (reachedMaxToolCalls) break;
    } else {
      // Model returned no tool calls — natural completion
      reachedMaxTurns = false;
      break;
    }
  }

  if (reachedMaxToolCalls) {
    logger.warn('Agent loop reached max tool calls', { maxToolCalls, totalToolCalls });
    yield {
      type: 'error',
      data: { reason: 'max_tool_calls_reached', maxToolCalls, totalToolCalls },
    };
  } else if (reachedMaxTurns) {
    logger.warn('Agent loop reached max turns without completing', { maxTurns });
    yield {
      type: 'error',
      data: { reason: 'max_turns_reached', maxTurns },
    };
  }
}


function _transformToolCalls(toolCalls: ToolDefinition[]): ToolCallDefinition[] {
  return toolCalls.map(toolCall => ({
    type: 'function' as const,
    function: {
      name: toolCall.name,
      description: toolCall.description,
      parameters: {
        ...toolCall.inputSchema,
      },
      strict: false,
    },
  }));
}

// Simple Helper Functions to fix specific tool calls
function prunedTools(rawToolCall: ToolCall) {
  const toolCall = { ...rawToolCall };

  if (toolCall.function.name === 'met-museum_get-museum-object') {
    const args = JSON.parse(toolCall.function.arguments);
    args.returnImage = false; // prevent images from being returned
    toolCall.function.arguments = JSON.stringify(args);
  }
  return toolCall;
}

/**
 * Shared handler for running MCP agent that can be used by different routers
 *
 * @param body - Request body matching RunAgentAPIRequestBodySchema format:
 *   - model: string - The LLM model to use (e.g., "openai/gpt-4o")
 *   - messages: Message[] - Array of conversation message(s). Example: [{"role": "user", "content": "Hello?"}]
 *   - enabledTools: string[] - List of tool names to enable
 *   - image: string - Docker image identifier for the sandbox
 *   - tags: Record<string, string> - Arbitrary tags for the request, for logging/tracing
 *   - max_turns?: number - Maximum number of agent loop iterations (defaults to 256)
 *   - systemPrompt?: string - System prompt for the agent
 *
 * @returns AsyncGenerator<AgentOutput> - Generator that yields either successful messages or errors during agent execution
 */

export async function handleRunMCPAgentEval(body: z.infer<typeof RunAgentAPIRequestBodySchema>) {
  // Use task_id from request body
  const taskId = body.task_id || 'unknown';

  // Extract prompt from user message
  const userMessage = body.messages.find(m => m.role === 'user');
  const prompt = userMessage?.content || 'No user message found';

  logger.info('=== NEW MCP EVAL REQUEST ===', {
    taskId,
    assignmentId: body.tags?.assignmentId,
    model: body.model,
    strategy: body.strategy || 'default (litellm)',
    llmBaseUrl: body.llm_base_url || config.llmBaseUrl + ' (from .env)',
    prompt: prompt.substring(0, 200) + (prompt.length > 200 ? '...' : ''),
    enabledToolsCount: body.enabledTools.length,
    enabledTools: body.enabledTools,
    messageCount: body.messages.length,
    tags: body.tags,
  });
  // Full conversation (can be large / contain sensitive content) goes to the
  // file-only verbose log, not INFO — keeps server logs lean.
  logger.verbose('=== NEW MCP EVAL REQUEST — full messages ===', { messages: body.messages });

  let mcpClient;
  if (body.image) {
    mcpClient = await createMCPClient({
      type: 'sandbox',
      image: body.image,
      tags: body.tags ?? {},
      enabledTools: body.enabledTools,
    });
  }

  if (!mcpClient) {
    throw new Error('Failed to create MCP client');
  }

  return runMcpAgent({
    mcpClient,
    model: body.model,
    messages: body.messages,
    maxTurns: body.max_turns,
    strategy: body.strategy,
    llmBaseUrl: body.llm_base_url,
    extraLlmParams: body.extra_llm_params,
    taskId,
    contextWindowManagement: body.context_window_management,
    toolOutputCap: body.tool_output_cap,
    maxToolCalls: body.max_tool_calls,
  });
}

/**
 * Handler for directly calling a tool via MCP sandbox client
 *
 * @param body - Request body matching CallToolAPIRequestBodySchema format:
 *   - image: string - Docker image identifier for the sandbox
 *   - tags: Record<string, string> - Arbitrary tags for the request, for logging/tracing
 *   - enabledTools: string[] - List of tool names to enable
 *   - toolName: string - Name of the tool to call
 *   - toolArgs: Record<string, any> - Arguments to pass to the tool
 *
 * @returns Promise<ToolCallOutput> - The result of the tool execution
 */
export async function handleCallMCPTool(body: z.infer<typeof CallToolAPIRequestBodySchema>) {
  const mcpClient = await createMCPClient({
    type: 'sandbox',
    image: body.image,
    tags: body.tags ?? {},
  });

  if (!mcpClient) {
    throw new Error('Failed to create MCP client');
  }

  return await mcpClient.callTool(body.toolName, body.toolArgs);
}
