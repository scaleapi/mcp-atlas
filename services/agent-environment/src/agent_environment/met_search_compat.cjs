/**
 * Search-only compatibility for metmuseum-mcp@0.9.2 after /v1/search retirement.
 * Loaded only in the Met MCP process via NODE_OPTIONS; no tool schema changes.
 * https://metmuseum.github.io/#migrating-from-v1search
 */
'use strict';

const ORIGIN = 'https://collectionapi.metmuseum.org';
const OLD_PATH = '/public/collection/v1/search';
const NEW_PATH = '/public/collection/v1.1/search';
const PAGE_SIZE = 500;
const MAX_RESULTS = 10000;
const TIMEOUT_MS = 45000;

function createCompatibleFetch(fetchImpl, timeoutMs = TIMEOUT_MS) {
  return async function compatibleFetch(input, init) {
    // The pinned adapter passes URL strings. Leave other fetch input types and
    // all unrelated requests untouched (including objects and departments).
    if (!(typeof input === 'string' || input instanceof URL)) {
      return fetchImpl(input, init);
    }
    const url = new URL(input);
    if (url.origin !== ORIGIN || url.pathname !== OLD_PATH ||
        (init?.method || 'GET').toUpperCase() !== 'GET') {
      return fetchImpl(input, init);
    }
    const deadline = AbortSignal.timeout(timeoutMs);
    const signal = init?.signal ? AbortSignal.any([init.signal, deadline]) : deadline;
    const ids = [];
    const seen = new Set();
    let total;
    url.pathname = NEW_PATH;
    for (let offset = 0; offset < MAX_RESULTS; offset += PAGE_SIZE) {
      signal.throwIfAborted();
      url.searchParams.set('offset', String(offset));
      url.searchParams.set('limit', String(PAGE_SIZE));
      const response = await fetchImpl(url.toString(), { ...init, signal });
      // Preserve HTTP failures (including rate limits), never return a partial
      // aggregate as a successful search or amplify failures with hidden retries.
      if (!response.ok) return response;
      const page = await response.json();
      signal.throwIfAborted();
      if (!Number.isSafeInteger(page?.total) || page.total < 0 ||
          !(Array.isArray(page.objectIDs) || (page.total === 0 && page.objectIDs === null))) {
        throw new Error('Met search returned an invalid page');
      }
      if (page.total > MAX_RESULTS) {
        throw new Error(`Met search matches ${page.total} objects; the replacement API exposes only the first ${MAX_RESULTS}. Narrow q or use title/departmentId filters. No partial results returned.`);
      }
      if (total !== undefined && total !== page.total) {
        throw new Error('Met search total changed during pagination; no partial results returned');
      }
      total = page.total;
      const pageIds = page.objectIDs || [];
      const expected = Math.min(PAGE_SIZE, total - offset);
      if (pageIds.length !== expected || pageIds.some(id => !Number.isSafeInteger(id) || id <= 0)) {
        throw new Error('Met search returned an incomplete or invalid page; no partial results returned');
      }
      for (const id of pageIds) {
        if (seen.has(id)) throw new Error('Met search repeated an ID during pagination; no partial results returned');
        seen.add(id);
        ids.push(id);
      }
      if (ids.length === total) {
        return new Response(JSON.stringify({ total, objectIDs: ids }), {
          status: 200, headers: { 'Content-Type': 'application/json' },
        });
      }
    }
    throw new Error('Met search exceeded the pagination bound');
  };
}

module.exports = { createCompatibleFetch };
globalThis.fetch = createCompatibleFetch(globalThis.fetch.bind(globalThis));
