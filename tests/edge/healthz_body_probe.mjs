// Exercise the production Worker with only its platform bindings supplied locally.
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
const source = (await readFile(process.argv[2], 'utf8')).replace(
  'import ROUTING from "./routing.json";',
  'const ROUTING = {static_first: [], edge_cached: {"/healthz": 10}, types: {}};',
);
const worker = (await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`)).default;
const realTimeout = AbortSignal.timeout;
try {
  for (const mode of ['headers-timeout', 'body-timeout', 'body-reset', 'healthy', 'empty', 'cached', 'refused']) {
    let calls = 0;
    let puts = 0;
    let controller;
    AbortSignal.timeout = () => (controller = new AbortController()).signal;
    globalThis.caches = {default: {
      match: async () => mode === 'cached' ? new Response('cached\n') : undefined,
      put: async () => { puts++; },
    }};
    globalThis.fetch = async (_request, options) => {
      calls++;
      assert.ok(options?.signal, `${mode}: escaped into an unbounded retry`);
      if (mode === 'headers-timeout') throw new DOMException('deadline', 'TimeoutError');
      if (mode === 'healthy') return new Response('ok\n');
      if (mode === 'empty') return new Response('');
      if (mode === 'refused') return new Response('busy\n', {status: 503});
      return new Response(new ReadableStream({
        start(stream) {
          stream.enqueue(new TextEncoder().encode('partial'));
          if (mode === 'body-timeout') {
            options.signal.addEventListener('abort', () => stream.error(options.signal.reason));
            // Headers have arrived, but consumption still waits when the deadline fires.
            setTimeout(() => controller.abort(new DOMException('deadline', 'TimeoutError')), 0);
          } else {
            setTimeout(() => stream.error(new TypeError('connection reset')), 0);
          }
        },
      }));
    };
    const response = await worker.fetch(new Request('https://example.test/healthz'), {
      ASSETS: {fetch: async () => assert.fail('healthz must never use a stored snapshot')},
    }, {});
    assert.equal(calls, mode === 'cached' ? 0 : 1, mode);
    assert.equal(response.status, ['healthy', 'empty', 'cached'].includes(mode) ? 200 : 503, mode);
    assert.equal(await response.text(), mode === 'healthy' ? 'ok\n' : mode === 'empty' ? '' : mode === 'cached' ? 'cached\n' : mode === 'refused' ? 'busy\n' : 'origin unavailable\n', mode);
    assert.equal(puts, ['healthy', 'empty'].includes(mode) ? 1 : 0, mode);
    if (mode.endsWith('timeout') || mode === 'body-reset') {
      assert.equal(response.headers.get('Cache-Control'), 'no-store', mode);
    }
    console.log(`${mode}: passed`);
  }
} finally {
  AbortSignal.timeout = realTimeout;
}
