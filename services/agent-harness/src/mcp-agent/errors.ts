import { z } from 'zod';

/**
 * One-line error text handed back to the model when a tool call fails.
 * ZodError.message is pretty-printed JSON, so its first line is a bare "[";
 * summarize the issues instead of truncating on the first newline.
 */
export function formatToolCallError(error: any): string {
  if (error instanceof z.ZodError) {
    return error.issues
      .map(issue => `${issue.path.join('.') || '<root>'}: ${issue.message}`)
      .join('; ');
  }
  return (error?.message || String(error)).split('\n')[0];
}

export class MCPClientValidationError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'MCPClientValidationError';
  }
}

export class MCPClientToolExecutionError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'MCPClientToolExecutionError';
  }
}

export class MCPClientInvalidToolError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'MCPClientInvalidToolError';
  }
}

export class MCPClientTimeoutError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'MCPClientTimeoutError';
  }
}
