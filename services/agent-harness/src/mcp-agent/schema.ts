import { z } from 'zod';

// ============================================================================
// Message Schemas
// ============================================================================

export const SystemMessageSchema = z.object({
  role: z.literal('system'),
  content: z.string(),
});

const UserMessageSchema = z.object({
  role: z.literal('user'),
  content: z.string(),
});

export const AssistantMessageSchema = z.object({
  role: z.literal('assistant'),
  content: z.string().nullish(),
  tool_calls: z
    .array(
      z.object({
        id: z.string(),
        type: z.literal('function'),
        function: z.object({
          name: z.string(),
          arguments: z.string(),
        }),
      }),
    )
    .nullish(),
  reasoning_content: z.string().nullish(),
});

const ToolCallOutputContentItemSchema = z.union([
  z.object({
    type: z.literal('text'),
    text: z.string(),
  }),
  z.object({
    type: z.literal('image'),
    image_url: z.object({
      url: z.string(),
    }),
  }),
]);

const ToolCallOutputMessageSchema = z.object({
  role: z.literal('tool'),
  tool_call_id: z.string(),
  content: z.array(ToolCallOutputContentItemSchema).default([]),
  metadata: z.record(z.any()).optional(),
});

export const MessageSchema = z.union([
  SystemMessageSchema,
  UserMessageSchema,
  AssistantMessageSchema,
  ToolCallOutputMessageSchema,
]);

// ============================================================================
// Request / Response Schemas
// ============================================================================

export const RunAgentAPIRequestBodySchema = z.object({
  image: z.string().optional(),
  tags: z.record(z.string(), z.string()).optional(),
  model: z.string(),
  messages: z.array(MessageSchema),
  enabledTools: z.array(z.string()),
  max_turns: z.number().optional(),
  strategy: z.string().optional(),
  task_id: z.string().optional(),
  llm_base_url: z.string().optional(),
  extra_llm_params: z.record(z.string(), z.any()).optional(),
  context_window_management: z.enum(['compact']).optional(),
  tool_output_cap: z.number().optional(),
  max_tool_calls: z.number().optional(),
});

export const CallToolAPIRequestBodySchema = z.object({
  image: z.string(),
  tags: z.record(z.string(), z.string()).optional(),
  toolName: z.string(),
  toolArgs: z.record(z.string(), z.any()),
});

export const CallToolResponseSchema = z.object({
  content: z.array(ToolCallOutputContentItemSchema).default([]),
  isError: z.boolean().default(false),
});

// ============================================================================
// Sandbox tool-disabling registry
// ============================================================================

export const SandboxToolsConfigSchema = z.array(
  z.object({
    image: z.string(),
    disabledTools: z.array(z.string()),
  }),
);
