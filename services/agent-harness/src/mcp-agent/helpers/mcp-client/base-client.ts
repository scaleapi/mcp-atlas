import { ToolDefinition, ToolCallOutput } from '../../types';

export abstract class MCPClient {
  abstract listTools(): Promise<ToolDefinition[]>;
  abstract callTool(toolName: string, args: any): Promise<ToolCallOutput>;
}
