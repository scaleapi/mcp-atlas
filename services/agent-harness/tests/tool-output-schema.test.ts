/**
 * Guard for the tool-result content-block union.
 *
 * A successful MCP tool call that returns a non-text block used to fail
 * CallToolResponseSchema.parse in the agent loop, and the ZodError was
 * truncated at its first newline, so the model received the literal string
 * "Error: [" and a working tool call was scored as a failure.
 *
 * Servers pinned in mcp_server_template.json that return these blocks:
 *   met-museum@0.9.2         get-museum-object, returnImage defaults to true  -> text + image
 *   desktop-commander@0.2.7  read_file / read_multiple_files on an image      -> text + image
 *   filesystem@2026.7.10     read_media_file                                  -> image | audio | resource
 */

import test from 'node:test';
import assert from 'node:assert/strict';
import { CallToolResponseSchema } from '../src/mcp-agent/schema';
import { formatToolCallError } from '../src/mcp-agent/errors';
import { describeNonTextContent } from '../src/mcp-agent/helpers/tool-content';

const BASE64_PNG = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAAAAAA6fptVAAAACklEQVR4nGMAAQAABQAB';

test('text + image result parses (met-museum, desktop-commander)', () => {
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

test('OpenAI image_url content is still accepted on inbound message history', () => {
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

test('a ZodError is summarized rather than truncated to "["', () => {
  let zodError: unknown;
  try {
    CallToolResponseSchema.parse({ content: [{ type: 'nonsense', foo: 1 }], isError: false });
  } catch (error) {
    zodError = error;
  }

  const message = formatToolCallError(zodError);
  assert.notEqual(message, '[');
  assert.match(message, /content\.0/);
});

test('non-Zod errors keep their first line', () => {
  const error = new Error('Failed to call tool foo: connection refused\n  at somewhere');
  assert.equal(formatToolCallError(error), 'Failed to call tool foo: connection refused');
});

test('non-text blocks become text so the LLM payload stays chat-completions shaped', () => {
  const described = describeNonTextContent([
    { type: 'text', text: 'Image file: /data/logo.png (image/png)\n' },
    { type: 'image', data: BASE64_PNG, mimeType: 'image/png' },
    { type: 'audio', data: BASE64_PNG, mimeType: 'audio/wav' },
    { type: 'resource_link', uri: 'file:///data/report.pdf', name: 'report.pdf', mimeType: 'application/pdf' },
    { type: 'resource', resource: { uri: 'file:///data/x.bin', mimeType: 'application/octet-stream', blob: BASE64_PNG } },
    { type: 'resource', resource: { uri: 'file:///data/notes.txt', mimeType: 'text/plain', text: 'inline text' } },
    { type: 'resource_link', uri: 'file:///data/unknown', name: 'unknown' },
    { type: 'image', image_url: { url: 'https://example.com/a.png' } },
  ]);

  assert.deepEqual(described, [
    { type: 'text', text: 'Image file: /data/logo.png (image/png)\n' },
    { type: 'text', text: '[image content omitted: image/png]' },
    { type: 'text', text: '[audio content omitted: audio/wav]' },
    { type: 'text', text: '[resource_link content omitted: application/pdf file:///data/report.pdf]' },
    { type: 'text', text: '[resource content omitted: application/octet-stream file:///data/x.bin]' },
    { type: 'text', text: 'inline text' },
    { type: 'text', text: '[resource_link content omitted: unknown type file:///data/unknown]' },
    { type: 'image', image_url: { url: 'https://example.com/a.png' } },
  ]);
});
