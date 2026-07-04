import { MCPClient } from './base-client';
import { SandboxMCPClient, SandboxMCPClientConfig } from './sandbox-client';

export async function createMCPClient(configs: SandboxMCPClientConfig): Promise<SandboxMCPClient> {
  return SandboxMCPClient.createIfNotExists(configs);
}

export { MCPClient };
