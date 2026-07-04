import { type ToolCallDefinition, type Message } from '../types';
import { LiteLLMAgentCompletionStrategy } from './strategies/litellm-strategy';

// Base interfaces that strategies can extend or redefine
export interface BaseCompletionRequest {
  model: string;
  messages: Message[];
  tools: ToolCallDefinition[];
  extraLlmParams?: Record<string, any>;
}

export interface BaseCompletionResult {
  message: Message;
}

export interface AgentCompletionStrategy<
  TRequest = BaseCompletionRequest,
  TResult = BaseCompletionResult,
> {
  createCompletion(request: TRequest): Promise<TResult>;
}

/**
 * Factory function for the agent completion strategy.
 *
 * Only LiteLLM is shipped here — it supports any model that's reachable via
 * a LiteLLM proxy (OpenAI, Anthropic, Gemini, self-hosted, etc.). To add a
 * new provider, implement AgentCompletionStrategy in a new file under
 * strategies/ and dispatch from here.
 */
export function getAgentCompletionStrategy(
  _model: string,
  _strategy?: string,
  _llmBaseUrl?: string,
): AgentCompletionStrategy<any, BaseCompletionResult> {
  return new LiteLLMAgentCompletionStrategy();
}
