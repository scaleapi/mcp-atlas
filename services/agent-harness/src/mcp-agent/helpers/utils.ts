import { logger } from '../../logger';
import { MCPClientTimeoutError } from '../errors';

// Lightweight stats collector shape used by the sandbox client for tool-response
// token histograms. Public default is a no-op (see sandbox-client.ts).
type StatsCollector = {
  incr: (metric: string, value: number, tags?: Record<string, string>) => void;
  histogram: (metric: string, value: number, tags?: Record<string, string>) => void;
};

export function promiseWithTimeout<T>(promise: Promise<T>, timeout: number): Promise<T> {
  return Promise.race([
    promise,
    new Promise<T>((_, reject) =>
      setTimeout(() => reject(new MCPClientTimeoutError(`Timeout after ${timeout}ms`)), timeout),
    ),
  ]);
}

/**
 * Helper to format JSON content with proper indentation, used in tool-result
 * logging. Falls through unchanged when content isn't JSON.
 */
export function formatJSONContent(content: any): any {
  if (!content) return content;

  if (Array.isArray(content)) {
    return content.map(item => {
      if (item.type === 'text' && item.text) {
        try {
          const parsed = JSON.parse(item.text);
          return { type: 'text' as const, text: JSON.stringify(parsed, null, 2) };
        } catch {
          return item;
        }
      }
      return item;
    });
  }

  if (typeof content === 'string') {
    try {
      const parsed = JSON.parse(content);
      return JSON.stringify(parsed, null, 2);
    } catch {
      return content;
    }
  }

  return content;
}

function estimateTokenCount(content: any): number {
  const contentString = typeof content === 'string' ? content : JSON.stringify(content);
  return Math.ceil(contentString.length / 3);
}

export function logToolResponseTokensFromContent(
  metrics: StatsCollector,
  content: any,
  toolName: string,
  additionalTags?: Record<string, string>,
): void {
  metrics.histogram('tool_response_tokens', estimateTokenCount(content), {
    tool_name: toolName,
    ...additionalTags,
  });
}

interface SandboxHealthCheckOptions {
  url: string;
  sandboxId: string;
  maxRetries?: number;
  retryDelayMs?: number;
  timeoutMs?: number;
  isHealthy?: (response: Response) => boolean;
}

export async function waitForSandboxHealth({
  url,
  sandboxId,
  maxRetries = 60,
  retryDelayMs = 2000,
  timeoutMs = 5000,
  isHealthy,
}: SandboxHealthCheckOptions): Promise<void> {
  const sleep = (ms: number) => new Promise(resolve => setTimeout(resolve, ms));
  const healthCheck = isHealthy ?? ((response: Response) => response.ok);

  let lastError: Error | null = null;
  let lastStatusCode: number | null = null;

  for (let i = 0; i < maxRetries; i++) {
    try {
      const controller = new AbortController();
      const timeoutId = setTimeout(() => controller.abort(), timeoutMs);

      const response = await fetch(url, { method: 'GET', signal: controller.signal });

      clearTimeout(timeoutId);

      if (healthCheck(response)) return;
      lastStatusCode = response.status;
    } catch (error) {
      lastError = error instanceof Error ? error : new Error(String(error));
    }

    if (i + 1 < maxRetries) await sleep(retryDelayMs);
  }

  logger.error('Sandbox failed to become healthy after all retries', {
    sandboxId,
    url,
    maxRetries,
    lastStatusCode,
    lastErrorMessage: lastError?.message,
  });

  throw new Error(
    `Sandbox ${sandboxId} failed health check after ${maxRetries} attempts. ` +
      `Last status: ${lastStatusCode ?? 'N/A'}. Last error: ${lastError?.message ?? 'N/A'}`,
  );
}
