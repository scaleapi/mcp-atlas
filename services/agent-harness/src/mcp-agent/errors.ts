import { z } from 'zod';

const MAX_TOOL_ERROR_CHARS = 300;

// Zod 3 collapses a failed union into one `invalid_union` issue whose message is
// the useless "Invalid input", so recurse into unionErrors for the real reason:
// the branches that matched the `type` discriminator, or the expected types.
function summarizeIssues(issues: z.ZodIssue[]): string[] {
  return issues.flatMap(issue => {
    const path = issue.path.join('.') || '<root>';
    if (issue.code !== 'invalid_union') return [`${path}: ${issue.message}`];

    const branches = issue.unionErrors.map(branch => branch.issues);
    const matched = branches.filter(b => !b.some(i => i.code === 'invalid_literal'));
    if (matched.length > 0) return summarizeIssues(matched.flat());

    const literals = branches.flat().filter(i => i.code === 'invalid_literal') as z.ZodInvalidLiteralIssue[];
    const received = JSON.stringify(literals[0]?.received);
    const expected = [...new Set(literals.map(i => JSON.stringify(i.expected)))].join(', ');
    return [`${path}: unrecognized type ${received}, expected one of ${expected}`];
  });
}

/**
 * One-line error text handed back to the model when a tool call fails.
 * ZodError.message is pretty-printed JSON, so its first line is a bare "[".
 */
export function formatToolCallError(error: any): string {
  if (error instanceof z.ZodError) {
    const summary = [...new Set(summarizeIssues(error.issues))].join('; ');
    return summary.length > MAX_TOOL_ERROR_CHARS
      ? `${summary.slice(0, MAX_TOOL_ERROR_CHARS)}… (${error.issues.length} issues)`
      : summary;
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
