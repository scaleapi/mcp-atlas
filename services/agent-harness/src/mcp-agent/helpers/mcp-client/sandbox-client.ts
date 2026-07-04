import _ from 'lodash';
import { ToolDefinition, ToolCallOutput } from '../../types';
import { MCPClient } from './base-client';
import {
  promiseWithTimeout,
  logToolResponseTokensFromContent,
  waitForSandboxHealth,
} from '../utils';
import { MCPClientToolExecutionError, MCPClientTimeoutError } from '../../errors';
import { loadSandboxToolsConfig } from '../mcp-server-configs';
import { createHash } from 'crypto';
import { logger } from '../../../logger';
import { config } from '../../../config';

const CACHE_TTL_SECS = 3600; // 1 hour (not used - caching disabled)
// Stub redis cache (caching disabled for local eval)
const redisCache = {
  get: async (_key: string): Promise<string | null> => null,
  set: async (_key: string, _value: string, _opts: { ttl: number }): Promise<void> => {},
};
// MCP sandbox URL — points to a running agent-environment container
const MCP_SANDBOX_URL = config.mcpSandboxUrl;
// Stub metrics collector (not needed for local eval)
const mcpClientMetrics = {
  incr: (..._args: any[]) => {},
  histogram: (..._args: any[]) => {},
};

export interface SandboxMCPClientConfig {
  type: 'sandbox';
  image: string;
  tags: Record<string, string>;
  enabledTools?: string[];
  enableAllTools?: boolean;
}

export class SandboxMCPClient extends MCPClient {
  private image: string;
  private tags: Record<string, string>;
  private sandboxId: string;
  private sandboxUrl: string;
  private enabledTools?: string[];
  private disabledTools?: string[];

  private enableCache: boolean = false; // Disabled - redis not available in standalone eval
  private toolCallTimeout: number = config.toolCallTimeoutMs; // configurable via TOOL_CALL_TIMEOUT_MS
  private listToolsTimeout: number = config.listToolsTimeoutMs; // configurable via LIST_TOOLS_TIMEOUT_MS

  private constructor({
    image,
    tags,
    sandboxId,
    sandboxUrl,
    enabledTools,
    disabledTools,
  }: {
    image: string;
    tags: Record<string, string>;
    sandboxId: string;
    sandboxUrl: string;
    enabledTools?: string[];
    disabledTools?: string[];
  }) {
    super();
    this.image = image;
    this.tags = tags;
    this.sandboxId = sandboxId;
    this.sandboxUrl = sandboxUrl;
    this.enabledTools = enabledTools;
    this.disabledTools = disabledTools;
  }

  public get sandboxInfo() {
    return {
      sandboxId: this.sandboxId,
      sandboxUrl: this.sandboxUrl,
      sandboxTags: this.tags,
    };
  }

  public static async createIfNotExists(config: SandboxMCPClientConfig): Promise<SandboxMCPClient> {
    // Skip loading disabled tools config when enableAllTools is true
    const disabledTools = config.enableAllTools
      ? []
      : (await loadSandboxToolsConfig({ image: config.image })).disabledTools;

    const sandboxUrl = MCP_SANDBOX_URL;

    await waitForSandboxHealth({
      url: sandboxUrl,
      sandboxId: 'local',
      maxRetries: 90,
      retryDelayMs: 2000,
      timeoutMs: 5000,
    });

    return new SandboxMCPClient({
      image: config.image,
      tags: config.tags,
      sandboxId: 'local',
      sandboxUrl,
      enabledTools: config.enabledTools,
      disabledTools,
    });
  }

  public async listTools(): Promise<ToolDefinition[]> {
    try {
      const startTime = Date.now();

      let tools = await this.getListToolsFromSandbox();

      // Filter by enabled tools if specified
      if (this.enabledTools && this.enabledTools.length > 0) {
        tools = tools.filter(tool => this.enabledTools!.includes(tool.name));
      }

      // Disabled tools are not visible to the contributors in the agent environment step
      tools = tools.map(tool => {
        if ((this.disabledTools ?? []).includes(tool.name)) {
          return { ...tool, disabled: true };
        }
        return tool;
      });

      const latency = (Date.now() - startTime) / 1000;
      mcpClientMetrics.histogram('list_tools_latency', latency, {
        image: this.image,
      });

      return tools;
    } catch (error) {
      mcpClientMetrics.incr('list_tools_failed', 1, {
        image: this.image,
      });
      logger.error(`Failed to list tools from sandbox: ${error.toString()}`);
      throw error;
    }
  }

  public async resetState(): Promise<void> {
    try {
      const startTime = Date.now();

      const response = await promiseWithTimeout(
        fetch(this.sandboxUrl + '/reset-state', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
          },
        }),
        this.toolCallTimeout,
      );

      if (!response.ok) {
        logger.error('Reset state HTTP error', {
          status_code: response.status,
          status_text: response.statusText,
          sandbox_id: this.sandboxId,
          image: this.image,
          sandbox_url: this.sandboxUrl,
          tags: this.tags,
        });
        throw new Error(`Failed to reset sandbox state: ${response.statusText}`);
      }

      const latency = (Date.now() - startTime) / 1000;

      logger.info('Successfully reset sandbox state', {
        sandbox_id: this.sandboxId,
        latency,
      });
    } catch (error) {
      mcpClientMetrics.incr('reset_state_failed', 1, {
        image: this.image,
      });
      logger.error(`Failed to reset sandbox state: ${error.toString()}`, {
        sandbox_id: this.sandboxId,
        image: this.image,
        sandbox_url: this.sandboxUrl,
        tags: this.tags,
      });

      if (error instanceof MCPClientTimeoutError) {
        throw error;
      }

      throw new Error(`Failed to reset sandbox state: ${error.message}`);
    }
  }

  public async callTool(toolName: string, args: any): Promise<ToolCallOutput> {
    try {
      mcpClientMetrics.incr('tool_call', 1, {
        image: this.image,
        tool_name: toolName,
      });
      const startTime = Date.now();

      // Build request body - always include tool_args (sandbox requires it, even if empty)
      const bodyToSend = {
        tool_name: toolName,
        tool_args: args || {},
      };

      const response = await promiseWithTimeout(
        fetch(this.sandboxUrl + '/call-tool', {
          method: 'POST',
          body: JSON.stringify(bodyToSend),
          headers: {
            'Content-Type': 'application/json',
          },
        }),
        this.toolCallTimeout,
      );

      let result: any;

      // Track HTTP response metrics
      mcpClientMetrics.incr('tool_call_http_response', 1, {
        status_code: response.status.toString(),
        image: this.image,
        tool_name: toolName,
      });

      if (!response.ok) {
        let errorText = '';
        try {
          errorText = await response.text();
        } catch (e) {
          errorText = `Unknown error ${e.toString()}`;
        }

        logger.error('Tool call HTTP error', {
          status_code: response.status,
          status_text: response.statusText,
          tool_name: toolName,
          error_text: errorText,
          sandbox_id: this.sandboxId,
          image: this.image,
          sandbox_url: this.sandboxUrl,
          tags: this.tags,
        });

        result = {
          content: [
            {
              type: 'text',
              text: errorText,
            },
          ],
          isError: true,
        };
      } else {
        const responseBody = await response.json();
        // Check if empty OR if all text content is empty strings
        const hasContent =
          Array.isArray(responseBody) &&
          responseBody.length > 0 &&
          responseBody.some(item => item.type === 'text' && item.text.trim() !== '');

        result = {
          content: hasContent ? responseBody : [{ type: 'text', text: 'success' }], // fallback for successfull but empty responses for tool calls
          isError: false,
        };
      }

      const latency = (Date.now() - startTime) / 1000;
      mcpClientMetrics.histogram('tool_call_latency', latency, {
        image: this.image,
        tool_name: toolName,
      });

      // Track token usage for tool response
      logToolResponseTokensFromContent(mcpClientMetrics, result.content, toolName, {
        image: this.image,
      });

      return result;
    } catch (error) {
      mcpClientMetrics.incr('tool_call_failed', 1, {
        image: this.image,
        tool_name: toolName,
      });
      logger.error(`Failed to call tool ${toolName} in sandbox: ${error.toString()}`, {
        tool_name: toolName,
        sandbox_id: this.sandboxId,
        image: this.image,
        sandbox_url: this.sandboxUrl,
        tags: this.tags,
      });

      // Return timeout errors as tool responses so the model can continue
      if (error instanceof MCPClientTimeoutError) {
        return {
          content: [{ type: 'text', text: `Tool call timed out after ${this.toolCallTimeout / 1000}s` }],
          isError: true,
        };
      }

      throw new MCPClientToolExecutionError(`Failed to call tool ${toolName}: ${error.message}`);
    }
  }

  private getListToolsCacheKey(): string {
    const hash = createHash('sha256')
      .update(`sandbox-mcp-client-list-tools:${this.image}`)
      .digest('hex');
    return hash;
  }

  private async getListToolsFromSandbox(): Promise<ToolDefinition[]> {
    const cacheKey = this.getListToolsCacheKey();
    if (this.enableCache) {
      const cachedValue = await redisCache.get(cacheKey);
      if (cachedValue) {
        return JSON.parse(cachedValue);
      }
    }

    const response = await promiseWithTimeout(
      fetch(this.sandboxUrl + '/list-tools', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
      }),
      this.listToolsTimeout,
    );

    if (!response.ok) {
      logger.error('List tools HTTP error', {
        status_code: response.status,
        status_text: response.statusText,
        sandbox_id: this.sandboxId,
        image: this.image,
        sandbox_url: this.sandboxUrl,
        tags: this.tags,
        endpoint: '/list-tools',
      });

      throw new Error(`Failed to fetch tools from sandbox: ${response.statusText}`);
    }

    const tools = (await response.json()) as ToolDefinition[];
    if (this.enableCache) {
      await redisCache.set(cacheKey, JSON.stringify(tools), { ttl: CACHE_TTL_SECS });
    }
    return tools;
  }
}

