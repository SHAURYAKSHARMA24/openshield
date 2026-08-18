// Dependency-free request lifecycle tests for api.js.
// Run with: node frontend/src/utils/api.test.mjs

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

function loadApiModule({ fetchImpl, timers } = {}) {
  let source = readFileSync(path.join(__dirname, 'api.js'), 'utf8');
  source = source.replace(
    /import\.meta\.env\.VITE_API_URL\s*\|\|\s*\(import\.meta\.env\.DEV \? '[^']*' : '[^']*'\)/,
    "'http://localhost:5000'",
  );
  assert.ok(!source.includes('import.meta'), 'failed to neutralize import.meta usage — test harness is stale');
  source = source.replace(/^export (const|class|async function) /gm, '$1 ');
  source = source.replace(/^export default api;$/m, '');
  source += `\nreturn {
    api, apiFetch, DEFAULT_REQUEST_TIMEOUT_MS, ApiRequestError,
    ApiTimeoutError, ApiCancellationError, ApiHttpError, ApiNetworkError,
  };`;

  const localStorageStub = { getItem: () => null, setItem: () => {} };
  const load = new Function(
    'localStorage', 'fetch', 'AbortController', 'setTimeout', 'clearTimeout', source,
  );
  return load(
    localStorageStub,
    fetchImpl || (() => Promise.reject(new Error('unexpected fetch'))),
    AbortController,
    timers?.setTimeout || setTimeout,
    timers?.clearTimeout || clearTimeout,
  );
}

function createTimers() {
  const pending = new Map();
  const delays = [];
  let nextId = 1;
  return {
    pending,
    delays,
    setTimeout(fn, delay) {
      const id = nextId++;
      pending.set(id, fn);
      delays.push(delay);
      return id;
    },
    clearTimeout(id) { pending.delete(id); },
    runNext() {
      const entry = pending.entries().next().value;
      assert.ok(entry, 'expected a pending timeout');
      const [id, fn] = entry;
      pending.delete(id);
      fn();
    },
  };
}

function jsonResponse(body, { ok = true, status = 200, statusText = 'OK' } = {}) {
  return { ok, status, statusText, json: async () => body };
}

function rejectWhenAborted(signal) {
  return new Promise((resolve, reject) => {
    const rejectAbort = () => reject(Object.assign(new Error('aborted'), { name: 'AbortError' }));
    if (signal.aborted) rejectAbort();
    else signal.addEventListener('abort', rejectAbort, { once: true });
  });
}

const tests = [];
function test(description, fn) { tests.push({ description, fn }); }

test('successful requests return JSON and clear the default timeout', async () => {
  const timers = createTimers();
  let requestSignal;
  const { apiFetch, DEFAULT_REQUEST_TIMEOUT_MS } = loadApiModule({
    timers,
    fetchImpl: async (_url, options) => {
      requestSignal = options.signal;
      return jsonResponse({ value: 42 });
    },
  });
  assert.deepEqual(await apiFetch('/score'), { value: 42 });
  assert.equal(timers.delays[0], DEFAULT_REQUEST_TIMEOUT_MS);
  assert.equal(timers.pending.size, 0);
  assert.equal(requestSignal.aborted, false);
});

test('non-2xx responses throw an HTTP error with status details', async () => {
  const timers = createTimers();
  const { apiFetch, ApiHttpError } = loadApiModule({
    timers,
    fetchImpl: async () => jsonResponse(null, { ok: false, status: 503, statusText: 'Unavailable' }),
  });
  await assert.rejects(apiFetch('/score'), (err) => {
    assert.ok(err instanceof ApiHttpError);
    assert.equal(err.code, 'HTTP_ERROR');
    assert.equal(err.status, 503);
    return true;
  });
  assert.equal(timers.pending.size, 0);
});

test('network failures are distinct from HTTP failures', async () => {
  const cause = new TypeError('Failed to fetch');
  const { apiFetch, ApiNetworkError } = loadApiModule({
    fetchImpl: async () => { throw cause; },
  });
  await assert.rejects(apiFetch('/score'), (err) => {
    assert.ok(err instanceof ApiNetworkError);
    assert.equal(err.code, 'NETWORK_ERROR');
    assert.equal(err.cause, cause);
    return true;
  });
});

test('response parsing errors are not mislabeled as network failures', async () => {
  const parseError = new SyntaxError('invalid JSON');
  const { apiFetch, ApiNetworkError } = loadApiModule({
    fetchImpl: async () => ({
      ...jsonResponse(null),
      json: async () => { throw parseError; },
    }),
  });
  await assert.rejects(apiFetch('/score'), (err) => {
    assert.equal(err, parseError);
    assert.equal(err instanceof ApiNetworkError, false);
    return true;
  });
});

test('the timeout aborts fetch and throws a typed timeout error', async () => {
  const timers = createTimers();
  const { apiFetch, ApiTimeoutError } = loadApiModule({
    timers,
    fetchImpl: async (_url, { signal }) => rejectWhenAborted(signal),
  });
  const request = apiFetch('/score', { timeoutMs: 250 });
  timers.runNext();
  await assert.rejects(request, (err) => {
    assert.ok(err instanceof ApiTimeoutError);
    assert.equal(err.code, 'TIMEOUT');
    assert.equal(err.timeoutMs, 250);
    return true;
  });
  assert.equal(timers.pending.size, 0);
});

test('caller cancellation is forwarded without being reported as a timeout', async () => {
  const timers = createTimers();
  const caller = new AbortController();
  let internalSignal;
  const { apiFetch, ApiCancellationError } = loadApiModule({
    timers,
    fetchImpl: async (_url, { signal }) => {
      internalSignal = signal;
      return rejectWhenAborted(signal);
    },
  });
  const request = apiFetch('/score', { signal: caller.signal });
  assert.notEqual(internalSignal, caller.signal);
  caller.abort();
  await assert.rejects(request, (err) => {
    assert.ok(err instanceof ApiCancellationError);
    assert.equal(err.code, 'CANCELLED');
    return true;
  });
  assert.equal(internalSignal.aborted, true);
  assert.equal(timers.pending.size, 0);
});

test('an already-aborted caller signal cancels before fetch can proceed', async () => {
  const caller = new AbortController();
  caller.abort();
  const { apiFetch, ApiCancellationError } = loadApiModule({
    fetchImpl: async (_url, { signal }) => rejectWhenAborted(signal),
  });
  await assert.rejects(apiFetch('/score', { signal: caller.signal }), ApiCancellationError);
});

test('public API methods accept operation-specific timeout overrides', async () => {
  const timers = createTimers();
  const { api } = loadApiModule({
    timers,
    fetchImpl: async () => jsonResponse({ score: 90 }),
  });
  assert.deepEqual(await api.getScore({ timeoutMs: 1250 }), { score: 90, max_score: 100 });
  assert.equal(timers.delays[0], 1250);
});

test('a completed request cannot be aborted by a stale timer', async () => {
  const timers = createTimers();
  let requestSignal;
  const { apiFetch } = loadApiModule({
    timers,
    fetchImpl: async (_url, { signal }) => {
      requestSignal = signal;
      return jsonResponse({ done: true });
    },
  });
  await apiFetch('/score', { timeoutMs: 10 });
  assert.equal(timers.pending.size, 0);
  assert.equal(requestSignal.aborted, false);
});

test('a completed request removes its caller abort listener', async () => {
  let activeListeners = 0;
  let registeredListener;
  const callerSignal = {
    aborted: false,
    addEventListener(_type, listener) {
      registeredListener = listener;
      activeListeners++;
    },
    removeEventListener(_type, listener) {
      if (listener === registeredListener) activeListeners--;
    },
  };
  const { apiFetch } = loadApiModule({
    fetchImpl: async () => jsonResponse({ done: true }),
  });
  await apiFetch('/score', { signal: callerSignal });
  assert.equal(activeListeners, 0);
});

test('timeouts can be disabled explicitly for a caller-managed request', async () => {
  const timers = createTimers();
  const { apiFetch } = loadApiModule({
    timers,
    fetchImpl: async () => jsonResponse({ done: true }),
  });
  await apiFetch('/score', { timeoutMs: null });
  assert.equal(timers.delays.length, 0);
});

let failures = 0;
for (const { description, fn } of tests) {
  try {
    await fn();
    console.log(`PASS: ${description}`);
  } catch (err) {
    failures++;
    console.error(`FAIL: ${description}\n  ${err.stack || err.message}`);
  }
}

if (failures > 0) {
  console.error(`\n${failures} test(s) failed`);
  process.exit(1);
}
console.log(`\nAll ${tests.length} API request tests passed`);
