import { SandboxToolsConfigSchema } from '../schema';
import { logger } from '../../logger';

/**
 * Stub for a per-image disabled-tools config lookup.
 *
 * By default all tools advertised by the sandbox are enabled. If you want
 * to restrict specific tools for a given image, return an array of configs
 * here matching SandboxToolsConfigSchema. Example:
 *
 *     return [
 *       { image: 'ghcr.io/scaleapi/mcp-atlas:1.2.5',
 *         disabledTools: ['dangerous_tool_1'] }
 *     ];
 */
const getDisabledToolsRegistry = async (_key: string): Promise<any> => {
  return [];
};

export async function loadSandboxToolsConfig({ image }: { image: string }) {
  let disabledTools: string[] = [];

  try {
    const registry = await getDisabledToolsRegistry('SandboxMCPConfig');
    const parsed = SandboxToolsConfigSchema.parse(registry);
    const config = parsed.find(item => item.image === image);
    disabledTools = config?.disabledTools ?? [];
  } catch (error) {
    logger.error(`Error loading sandbox tools config: ${(error as Error).message}`);
  }

  return { disabledTools };
}
