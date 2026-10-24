import { test } from 'node:test';
import assert from 'node:assert/strict';
import { ApiError, parseModelUri, request } from './api.js';

test('deployment URI requires an immutable version and derives the logical model name', () => {
  assert.deepEqual(parseModelUri('  models:/risk.score-1/18  '), { uri: 'models:/risk.score-1/18', model: 'risk.score-1', version: '18' });
  for (const uri of ['models:/credit/Production', 'models:/credit@champion', 'runs:/id/model', 'models:/credit/1/extra', 'models:/bad name/1', 'models:/credit/']) assert.equal(parseModelUri(uri), null, uri);
});

test('API errors retain safe messages and codes and reject HTML responses', async t => {
  t.mock.method(globalThis, 'fetch', async () => new Response(JSON.stringify({ error: { message: 'Model unavailable', code: 'MODEL_NOT_READY' } }), { status: 503 }));
  await assert.rejects(request('/test'), error => error instanceof ApiError && error.status === 503 && error.code === 'MODEL_NOT_READY');
  globalThis.fetch.mock.mockImplementation(async () => new Response('<html>Proxy error</html>'));
  await assert.rejects(request('/test'), /некорректный ответ/);
});

test('request cancellation distinguishes a timeout from changing the selected model', async t => {
  t.mock.method(globalThis, 'fetch', (_, { signal }) => new Promise((resolve, reject) => signal.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')), { once: true })));
  await assert.rejects(request('/slow', { timeout: 5 }), /не ответил вовремя/);
  const controller = new AbortController();
  const promise = request('/slow', { signal: controller.signal });
  controller.abort();
  await assert.rejects(promise, { name: 'AbortError' });
});
