'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { createCompatibleFetch } = require('../src/agent_environment/met_search_compat.cjs');
const OLD = 'https://collectionapi.metmuseum.org/public/collection/v1/search';
const response = body => new Response(JSON.stringify(body), { headers: { 'Content-Type': 'application/json' } });
const ids = (start, count) => Array.from({ length: count }, (_, i) => start + i);

test('migrates only search, preserves filters and gathers all pages in order', async () => {
  const calls = [];
  const fetch = createCompatibleFetch(async (input, init) => {
    const url = new URL(input); calls.push(url);
    assert.equal(url.pathname, '/public/collection/v1.1/search');
    assert.equal(url.searchParams.get('q'), 'cat & dog');
    assert.equal(url.searchParams.get('hasImages'), 'true');
    assert.equal(url.searchParams.get('departmentId'), '6');
    assert.equal(url.searchParams.get('limit'), '500');
    assert.equal(init.headers.Accept, 'application/json');
    const offset = Number(url.searchParams.get('offset'));
    return response({ total: 1201, objectIDs: ids(offset + 1, Math.min(500, 1201 - offset)) });
  });
  const actual = await (await fetch(OLD + '?q=cat+%26+dog&hasImages=true&departmentId=6', {
    headers: { Accept: 'application/json' },
  })).json();
  assert.deepEqual(actual, { total: 1201, objectIDs: ids(1, 1201) });
  assert.deepEqual(calls.map(u => u.searchParams.get('offset')), ['0', '500', '1000']);
});

test('leaves object, departments, other hosts, direct v1.1 and POST untouched', async () => {
  for (const [url, init] of [
    ['https://collectionapi.metmuseum.org/public/collection/v1/objects/32907'],
    ['https://collectionapi.metmuseum.org/public/collection/v1/departments'],
    ['https://example.com/public/collection/v1/search?q=x'],
    ['https://collectionapi.metmuseum.org/public/collection/v1.1/search?q=x'],
    [OLD, { method: 'POST' }],
  ]) {
    const sentinel = new Response('untouched');
    const fetch = createCompatibleFetch(async (u, options) => {
      assert.equal(u, url); assert.equal(options, init); return sentinel;
    });
    assert.equal(await fetch(url, init), sentinel);
  }
});

test('zero results accepts both provider empty forms', async () => {
  for (const objectIDs of [null, []]) {
    const fetch = createCompatibleFetch(async () => response({ total: 0, objectIDs }));
    assert.deepEqual(await (await fetch(new URL(OLD))).json(), { total: 0, objectIDs: [] });
  }
});

test('allows exactly 10000 results, using at most 20 sequential pages', async () => {
  let calls = 0;
  const fetch = createCompatibleFetch(async input => {
    calls++;
    const offset = Number(new URL(input).searchParams.get('offset'));
    return response({ total: 10000, objectIDs: ids(offset + 1, 500) });
  });
  const result = await (await fetch(OLD)).json();
  assert.equal(result.objectIDs.length, 10000); assert.equal(calls, 20);
});

test('fails explicitly above the provider window, without silent truncation', async () => {
  let calls = 0;
  const fetch = createCompatibleFetch(async () => { calls++; return response({ total: 10001, objectIDs: ids(1, 500) }); });
  await assert.rejects(fetch(OLD), /Narrow q/); assert.equal(calls, 1);
});

test('fails on missing or short pages, bad totals and invalid IDs', async () => {
  for (const body of [{}, { total: -1, objectIDs: [] }, { total: '1', objectIDs: [1] },
    { total: 3, objectIDs: [1] }, { total: 1, objectIDs: [null] },
    { total: 1, objectIDs: [1.5] }, { total: 1, objectIDs: null }]) {
    const fetch = createCompatibleFetch(async () => response(body));
    await assert.rejects(fetch(OLD), /invalid|incomplete/);
  }
});

test('fails rather than de-duplicating or silently accepting changing totals', async () => {
  for (const second of [{ total: 501, objectIDs: [1] }, { total: 502, objectIDs: [501, 502] }]) {
    let n = 0;
    const fetch = createCompatibleFetch(async () => response(n++ ? second : { total: 501, objectIDs: ids(1, 500) }));
    await assert.rejects(fetch(OLD), /repeated|changed/);
  }
});

test('second-page HTTP failure propagates without retries or partial success', async () => {
  for (const status of [401, 410, 429, 500, 503]) {
    let calls = 0;
    const failure = new Response('unavailable', { status });
    const fetch = createCompatibleFetch(async () => calls++ ? failure : response({ total: 501, objectIDs: ids(1, 500) }));
    assert.equal(await fetch(OLD), failure); assert.equal(calls, 2);
  }
});

test('caller cancellation and shared timeout stop the page sequence', async () => {
  const controller = new AbortController(); controller.abort();
  let calls = 0;
  const neverCalled = createCompatibleFetch(async () => { calls++; });
  await assert.rejects(neverCalled(OLD, { signal: controller.signal }), { name: 'AbortError' });
  assert.equal(calls, 0);
  const timeout = createCompatibleFetch(async (_, init) => {
    await new Promise(resolve => setTimeout(resolve, 15));
    init.signal.throwIfAborted();
    return response({ total: 0, objectIDs: [] });
  }, 5);
  await assert.rejects(timeout(OLD), { name: 'TimeoutError' });
});

test('malformed JSON and network errors do not become successful empty results', async () => {
  await assert.rejects(createCompatibleFetch(async () => new Response('not JSON'))(OLD));
  await assert.rejects(createCompatibleFetch(async () => { throw new Error('network'); })(OLD), /network/);
});

test('template pins adapter and scopes preload to Met only', () => {
  const config = require('../src/agent_environment/mcp_server_template.json').mcpServers;
  assert.deepEqual(config['met-museum'].args, ['metmuseum-mcp@0.9.2']);
  assert.equal(config['met-museum'].env.NODE_OPTIONS, '--require=/agent-environment/src/agent_environment/met_search_compat.cjs');
  assert.equal(Object.values(config).filter(s => JSON.stringify(s).includes('met_search_compat.cjs')).length, 1);
});
