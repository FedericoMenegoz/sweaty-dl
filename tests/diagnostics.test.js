import test from 'node:test';
import assert from 'node:assert/strict';
import { createLoggedInvoker } from '../src/diagnostics.js';

test('IPC diagnostics correlate failures without recording arguments or returned media URLs', async () => {
  const records = [];
  const invoke = createLoggedInvoker(async () => { throw 'Download non riuscito.'; }, (...args) => records.push(args), () => 'trace-one');
  await assert.rejects(invoke('download_audio', { url: 'https://cdn.example/stream?token=SECRET', destination: '/private/user' }), error => error.includes('[rif. trace-one]'));
  assert.equal(records[0][3], 'trace-one');
  assert.equal(records[1][0], 'ipc_failed');
  assert.equal(records[1][3], 'trace-one');
  assert.ok(!JSON.stringify(records).includes('SECRET'));
  assert.ok(!JSON.stringify(records).includes('/private/user'));
});

test('native request identifiers are retained and media results are not logged', async () => {
  const records = [], calls = [];
  const invoke = createLoggedInvoker(async (command, args) => { calls.push(args); return { stream_url: 'https://cdn.example/stream?token=SECRET', results: [1, 2] }; }, (...args) => records.push(args));
  const output = await invoke('prepare_preview', { requestId: 'preview-one' });
  assert.match(output.stream_url, /SECRET/);
  assert.equal(calls[0].traceId, 'preview-one');
  assert.equal(records[1][1].result_count, 2);
  assert.ok(!JSON.stringify(records).includes('SECRET'));
});

test('logging failures never change an otherwise successful operation', async () => {
  const invoke = createLoggedInvoker(async () => 'ok', () => { throw new Error('Log unavailable'); });
  assert.equal(await invoke('search'), 'ok');
});
