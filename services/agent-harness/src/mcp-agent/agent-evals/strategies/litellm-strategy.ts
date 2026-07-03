import axios from 'axios';
import { config } from '../../../config';
import { AssistantMessageSchema } from '../../schema';
import {
  AgentCompletionStrategy,
  BaseCompletionRequest,
  BaseCompletionResult,
} from '../completion-strategy';
import { type Message } from '../../types';
import { logger } from '../../../logger';

/**
 * Strip unsupported JSON schema fields from tool definitions.
 * Fireworks models reject fields like "format": "ipv4" in tool parameter schemas.
 * Only applied for fireworks_ai/ models — other providers are unaffected.
 */
function stripUnsupportedSchemaFields(tools: any[], model: string): any[] {
  if (!model.startsWith('fireworks_ai/')) return tools;
  return JSON.parse(JSON.stringify(tools, (key, value) => {
    if (key === 'format' && typeof value === 'string') return undefined;
    return value;
  }));
}

const CHAT_LLM_API_KEY = config.llmApiKey;
const LLM_BASE_URL = config.llmBaseUrl;
const LLM_ENDPOINT = `${LLM_BASE_URL}/v1/chat/completions`;
const MAX_BODY_SIZE = 100_000_000;
const LLM_TIMEOUT = config.llmTimeoutMs; // configurable via LLM_TIMEOUT_MS — raise for heavy reasoning
const MAX_429_RETRIES = 5;

function sleep(ms: number): Promise<void> {
  return new Promise(resolve => setTimeout(resolve, ms));
}

// LiteLLM uses the base result type (internal types)
type LiteLLMCompletionResult = BaseCompletionResult;
type LiteLLMCompletionRequest = BaseCompletionRequest;

export class LiteLLMAgentCompletionStrategy
  implements AgentCompletionStrategy<BaseCompletionRequest, BaseCompletionResult>
{
  async createCompletion(request: LiteLLMCompletionRequest): Promise<LiteLLMCompletionResult> {
    // Per-request API key override. Pulled out of extraLlmParams so it's not
    // forwarded as a body field. Falls back to config.llmApiKey.
    const { api_key: overrideApiKey, ...extraLlmParamsRest } =
      (request.extraLlmParams || {}) as Record<string, any>;
    const apiKey = overrideApiKey || CHAT_LLM_API_KEY;

    const payload: Record<string, any> = {
      model: request.model,
      messages: await this.addSystemPromptIfNeeded(request.messages, request.model),
      tools: stripUnsupportedSchemaFields(request.tools, request.model),
      // Minimal, standard OpenAI Chat-Completions payload — no provider-specific
      // fields — so it works with any compatible endpoint (OpenAI, vLLM/TGI/SGLang,
      // LiteLLM proxy). Add provider extras (incl. LiteLLM `metadata`) via extra_llm_params.
      ...extraLlmParamsRest,
    };

    // Log summary to console, full payload to file only
    logger.info('LiteLLM Strategy - Sending payload to LLM', {
      endpoint: LLM_ENDPOINT,
      model: payload.model,
      messageCount: payload.messages.length,
      toolCount: payload.tools?.length || 0,
    });
    logger.verbose('LiteLLM Strategy - Full payload', {
      payload: JSON.stringify(payload),
    });

    for (let attempt = 0; attempt <= MAX_429_RETRIES; attempt++) {
      try {
        const response = await axios.post(LLM_ENDPOINT, payload, {
          headers: {
            'Content-Type': 'application/json',
            Authorization: `Bearer ${apiKey}`,
          },
          timeout: LLM_TIMEOUT,
          maxContentLength: MAX_BODY_SIZE,
          maxBodyLength: MAX_BODY_SIZE,
        });

        if (response.status !== 200) {
          throw new Error(`HTTP ${response.status}`);
        }

        const message = AssistantMessageSchema.parse(response.data.choices[0].message);

        return {
          message,
        };
      } catch (error: any) {
        if (error.response?.status === 429 && attempt < MAX_429_RETRIES) {
          const retryAfter = error.response?.headers?.['retry-after'];
          const resetTime = error.response?.data?.error?.message?.match(/resets at: (.+)/)?.[1];
          let waitMs: number;

          if (retryAfter) {
            waitMs = parseInt(retryAfter, 10) * 1000;
          } else if (resetTime) {
            waitMs = Math.max(new Date(resetTime).getTime() - Date.now(), 1000);
          } else {
            waitMs = Math.min(2 ** attempt * 1000, 60000) + Math.random() * 2000;
          }

          logger.warn(`Rate limited (429), retrying in ${Math.round(waitMs / 1000)}s`, {
            attempt: attempt + 1,
            model: payload.model,
            waitMs,
            errorMessage: error.response?.data?.error?.message || null,
          });
          await sleep(waitMs);
          continue;
        }

        // Auth errors (401) — proxy token cache may need time to sync
        if (error.response?.status === 401 && attempt < MAX_429_RETRIES) {
          logger.warn(`Auth error (401), retrying in 30s`, {
            attempt: attempt + 1,
            model: payload.model,
            error: error.response?.data?.error?.message?.slice(0, 150),
          });
          await sleep(30_000);
          continue;
        }

        throw error;
      }
    }
    // Unreachable in practice — the retry loop always either returns or throws.
    throw new Error('LLM call failed after exhausting retries.');
  }

  private async addSystemPromptIfNeeded(messages: Message[], model: string): Promise<Message[]> {
    const hasSystemMessage = messages.some(message => message.role === 'system');

    // For gpt-5, add an empty system prompt if none exists.
    if (!hasSystemMessage && model === 'openai/gpt-5') {
      const systemPrompt = '';
      return [{ role: 'system', content: systemPrompt }, ...messages];
    }

    return messages;
  }
}
