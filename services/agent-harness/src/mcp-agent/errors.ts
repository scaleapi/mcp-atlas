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
