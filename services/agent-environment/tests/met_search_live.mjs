/** Opt-in live test: node met_search_live.mjs /path/to/node_modules/metmuseum-mcp.
 * No credentials required. Starts the real pinned stdio MCP server twice;
 * verifies unchanged tool schemas and the migrated search/object operations.
 */
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { resolve, dirname } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { readFile } from 'node:fs/promises';

if (!process.argv[2]) throw new Error('Usage: node met_search_live.mjs /path/to/node_modules/metmuseum-mcp');
const root = resolve(process.argv[2]);
const metadata = JSON.parse(await readFile(resolve(root, 'package.json'), 'utf8'));
assert.equal(metadata.name, 'metmuseum-mcp');
assert.equal(metadata.version, '0.9.2');
const require = createRequire(resolve(root, 'package.json'));
const { Client } = await import(pathToFileURL(require.resolve('@modelcontextprotocol/sdk/client/index.js')));
const { StdioClientTransport } = await import(pathToFileURL(require.resolve('@modelcontextprotocol/sdk/client/stdio.js')));
const preload = resolve(dirname(fileURLToPath(import.meta.url)), '../src/agent_environment/met_search_compat.cjs');
async function connect(migrated) {
  const transport = new StdioClientTransport({
    command: process.execPath,
    args: [...(migrated ? ['--require', preload] : []), resolve(root, 'dist/index.js')],
    env: { PATH: process.env.PATH || '' }, stderr: 'inherit',
  });
  const client = new Client({ name: 'met-search-compat-test', version: '1.0.0' });
  try { await client.connect(transport); } catch (e) { await transport.close(); throw e; }
  return client;
}
const original = await connect(false);
let schemas;
try { schemas = (await original.listTools()).tools; } finally { await original.close(); }
const migrated = await connect(true);
try {
  assert.deepEqual((await migrated.listTools()).tools, schemas);
  console.log(JSON.stringify({ check: 'unchanged_tool_schemas', ok: true, tools: schemas.map(t => t.name) }));
  const emptyQuery = 'zzzz_mcp_no_matching_art_8675309';
  for (const q of ['sunflowers', 'cat', 'Hercules holding a club', emptyQuery]) {
    const result = await migrated.callTool({ name: 'search-museum-objects', arguments: { q } });
    assert.ok(!result.isError, JSON.stringify(result));
    const text = result.content.filter(c => c.type === 'text').map(c => c.text).join('\n');
    if (q === emptyQuery) {
      assert.equal(text, 'No objects found', 'The deliberately unmatched query must return no results');
      console.log(JSON.stringify({ check: 'search', q, total: 0, ok: true })); continue;
    }
    assert.notEqual(text, 'No objects found', `Expected results for ${q}`);
    const match = text.match(/^Total objects found: (\d+)\nObject IDs: (.*)$/s);
    assert.ok(match, text.slice(0, 200));
    const total = Number(match[1]);
    const ids = match[2].split(', ').map(Number);
    assert.equal(ids.length, total); assert.equal(new Set(ids).size, total);
    if (q === 'cat') assert.ok(total > 500, 'Live pagination probe needs >500 matches');
    console.log(JSON.stringify({ check: 'search', q, total, returned: ids.length, ok: true }));
  }
  for (const [name, args] of [['list-departments', {}], ['get-museum-object', { objectId: 32907, returnImage: false }]]) {
    const result = await migrated.callTool({ name, arguments: args });
    assert.ok(!result.isError, JSON.stringify(result));
    assert.ok(result.content.length);
    console.log(JSON.stringify({ check: name, ok: true }));
  }
} finally { await migrated.close(); }
