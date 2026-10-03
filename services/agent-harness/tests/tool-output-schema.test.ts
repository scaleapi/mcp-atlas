/**
 * Guard for the tool-result content-block union. A successful MCP tool call that
 * returned a non-text block used to fail CallToolResponseSchema.parse, and the
 * ZodError was truncated at its first newline, so the model received the literal
 * string "Error: [" and a working tool call was scored as a failure.
 */

import test from 'node:test';
import assert from 'node:assert/strict';
import { CallToolResponseSchema, RunAgentAPIRequestBodySchema } from '../src/mcp-agent/schema';
import { formatToolCallError } from '../src/mcp-agent/errors';
import { describeNonTextContent } from '../src/mcp-agent/helpers/tool-content';

const BASE64_PNG = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAAAAAA6fptVAAAACklEQVR4nGMAAQAABQAB';

test('text + image result parses (desktop-commander read_file)', () => {
  const parsed = CallToolResponseSchema.parse({
    content: [
      { type: 'text', text: 'Image file: /data/repos/storyteller/example/example-1.png (image/png)\n' },
      { type: 'image', data: BASE64_PNG, mimeType: 'image/png' },
    ],
    isError: false,
  });

  assert.equal(parsed.content.length, 2);
  assert.deepEqual(parsed.content[1], { type: 'image', data: BASE64_PNG, mimeType: 'image/png' });
});

test('audio, resource_link and embedded resource results parse (filesystem read_media_file)', () => {
  const blocks = [
    { type: 'audio', data: BASE64_PNG, mimeType: 'audio/wav' },
    { type: 'resource_link', uri: 'file:///data/report.pdf', name: 'report.pdf', mimeType: 'application/pdf' },
    {
      type: 'resource',
      resource: { uri: 'file:///data/report.pdf', mimeType: 'application/pdf', blob: BASE64_PNG },
    },
    { type: 'resource', resource: { uri: 'file:///data/notes.txt', mimeType: 'text/plain', text: 'hi' } },
  ];

  const parsed = CallToolResponseSchema.parse({ content: blocks, isError: false });
  assert.deepEqual(parsed.content, blocks);
});

test('OpenAI image_url content is still accepted on tool results', () => {
  const parsed = CallToolResponseSchema.parse({
    content: [{ type: 'image', image_url: { url: 'https://example.com/a.png' } }],
    isError: false,
  });

  assert.deepEqual(parsed.content[0], { type: 'image', image_url: { url: 'https://example.com/a.png' } });
});

test('genuinely invalid content is still rejected', () => {
  assert.throws(() =>
    CallToolResponseSchema.parse({ content: [{ type: 'nonsense', foo: 1 }], isError: false }),
  );
});

function parseError(content: unknown[]): string {
  try {
    CallToolResponseSchema.parse({ content, isError: false });
  } catch (error) {
    return formatToolCallError(error);
  }
  return assert.fail('expected a ZodError');
}

test('a union failure reports the real reason, not "Invalid input"', () => {
  const message = parseError([{ type: 'text', text: 'ok' }, { type: 'nonsense', foo: 1 }]);

  assert.doesNotMatch(message, /Invalid input/);
  assert.match(message, /content\.1: unrecognized type "nonsense"/);
  assert.match(message, /expected one of .*"resource_link"/);
});

test('a union failure inside a matching block names the missing field', () => {
  const message = parseError([{ type: 'image', data: BASE64_PNG }]);

  assert.doesNotMatch(message, /Invalid input/);
  assert.match(message, /content\.0\.mimeType: Required/);
});

test('many bad blocks do not produce an unbounded error string', () => {
  const message = parseError(Array.from({ length: 40 }, () => ({ type: 'bogus' })));

  assert.ok(message.length < 400, `error string was ${message.length} chars`);
  assert.match(message, /\(40 issues\)$/);
});

test('non-Zod errors keep their first line', () => {
  const error = new Error('Failed to call tool foo: connection refused\n  at somewhere');
  assert.equal(formatToolCallError(error), 'Failed to call tool foo: connection refused');
});

test('inbound tool messages stay narrow: raw MCP blocks are rejected', () => {
  const body = (content: unknown[]) => ({
    model: 'openai/gpt-4o',
    enabledTools: [],
    messages: [{ role: 'tool', tool_call_id: 'call_1', content }],
  });

  // Would otherwise be forwarded to the provider verbatim as megabytes of base64.
  assert.throws(() => RunAgentAPIRequestBodySchema.parse(body([
    { type: 'image', data: BASE64_PNG, mimeType: 'image/png' },
  ])));
  assert.throws(() => RunAgentAPIRequestBodySchema.parse(body([
    { type: 'resource', resource: { uri: 'file:///data/x.bin', blob: BASE64_PNG } },
  ])));

  const parsed = RunAgentAPIRequestBodySchema.parse(body([
    { type: 'text', text: 'hi' },
    { type: 'image', image_url: { url: 'https://example.com/a.png' } },
  ]));
  assert.equal((parsed.messages[0] as any).content.length, 2);
});

test('non-text blocks become text so the LLM payload stays chat-completions shaped', () => {
  const textItem = { type: 'text', text: 'Image file: /data/logo.png (image/png)\n' };
  const described = describeNonTextContent([
    textItem,
    { type: 'image', data: BASE64_PNG, mimeType: 'image/png' },
    { type: 'audio', data: BASE64_PNG, mimeType: 'audio/wav' },
    { type: 'resource_link', uri: 'file:///data/report.pdf', name: 'report.pdf', mimeType: 'application/pdf' },
    { type: 'resource', resource: { uri: 'file:///data/x.bin', mimeType: 'application/octet-stream', blob: BASE64_PNG } },
    { type: 'resource', resource: { uri: 'file:///data/notes.txt', mimeType: 'text/plain', text: 'inline text' } },
    { type: 'resource_link', uri: 'file:///data/unknown', name: 'unknown' },
    { type: 'image', image_url: { url: 'https://example.com/a.png' } },
  ]);

  assert.deepEqual(described, [
    textItem,
    { type: 'text', text: '[image content omitted: image/png]' },
    { type: 'text', text: '[audio content omitted: audio/wav]' },
    { type: 'text', text: '[resource_link content omitted: application/pdf file:///data/report.pdf]' },
    { type: 'text', text: '[resource content omitted: application/octet-stream file:///data/x.bin]' },
    { type: 'text', text: 'inline text' },
    { type: 'text', text: '[resource_link content omitted: unknown type file:///data/unknown]' },
    { type: 'image', image_url: { url: 'https://example.com/a.png' } },
  ]);
  // Text-only results must pass through untouched, not be copied.
  assert.equal(described[0], textItem);
});
