const { test } = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const vm = require('node:vm');

const source = readFileSync(`${__dirname}/../app/static/api.js`, 'utf8').replace(/^export /gm, '');
function setup(totalChunks = 2) {
  let now = 0;
  let timerId = 0;
  const timers = new Map();
  const requests = [];
  const calls = [];
  const storage = new Map();
  class Events {
    handlers = new Map();
    addEventListener(name, fn) { this.handlers.set(name, fn); }
    emit(name, event = {}) { this.handlers.get(name)?.(event); }
  }
  class XHR extends Events {
    upload = new Events();
    open(method, url) { this.url = url; }
    setRequestHeader() {}
    send() { requests.push(this); }
    abort() { this.aborted = true; this.emit('abort'); this.emit('loadend'); }
    finish(status = 200) {
      this.status = status;
      this.responseText = '{}';
      this.emit('load');
      this.emit('loadend');
    }
    progress(loaded) { this.upload.emit('progress', { lengthComputable: true, loaded, total: 100 }); }
  }
  const context = vm.createContext({
    XMLHttpRequest: XHR, Headers, FormData, URLSearchParams,
    setTimeout(fn, delay) { const id = ++timerId; timers.set(id, { fn, at: now + delay }); return id; },
    clearTimeout(id) { timers.delete(id); },
    localStorage: { getItem: k => storage.get(k), setItem: (k, v) => storage.set(k, v), removeItem: k => storage.delete(k) },
    fetch: async url => {
      calls.push(url);
      const body = url.includes('/init')
        ? { upload_id: 'session', total_chunks: totalChunks, chunk_size: 100, received: [] }
        : { name: 'photo.tiff' };
      return { ok: true, headers: new Headers({ 'Content-Type': 'application/json' }), json: async () => body };
    },
  });
  vm.runInContext(source, context);
  const flush = async () => { for (let i = 0; i < 30; i++) await Promise.resolve(); };
  return {
    requests, calls, timers, storage, flush,
    start: () => context.uploadFileChunked('', { name: 'photo.tiff', size: totalChunks * 100, slice: () => ({}) }),
    async advance(ms) {
      now += ms;
      for (const [id, timer] of [...timers]) {
        if (timer.at <= now) { timers.delete(id); timer.fn(); }
      }
      await flush();
    },
  };
}

test('stalled chunk is retried without resending a completed sibling', async () => {
  const h = setup();
  const upload = h.start();
  await h.flush();
  h.requests[0].finish();
  h.requests[1].progress(20);
  await h.advance(60000);
  assert.equal(h.requests[1].aborted, true, 'stalled request must be aborted after one minute');
  await h.advance(300);
  assert.equal(h.requests.length, 3);
  assert.match(h.requests[2].url, /index=1/);
  h.requests[2].finish();
  await upload.promise;
  assert.equal(h.calls.filter(url => url.includes('/complete')).length, 1);
  assert.equal(h.timers.size, 0);
  assert.equal(h.storage.size, 0);
});

test('continued byte progress keeps a slow chunk alive', async () => {
  const h = setup(1);
  const upload = h.start();
  await h.flush();
  await h.advance(45000);
  h.requests[0].progress(10);
  await h.advance(45000);
  assert.equal(h.requests[0].aborted, undefined);
  h.requests[0].finish();
  await upload.promise;
  assert.equal(h.timers.size, 0);
});

test('repeated stalls terminate after three attempts and preserve resume data', async () => {
  const h = setup(1);
  const upload = h.start();
  const failure = assert.rejects(upload.promise, /stalled/i);
  await h.flush();
  for (let attempt = 1; attempt <= 3; attempt++) {
    await h.advance(60000);
    assert.equal(h.requests.at(-1).aborted, true);
    await h.advance(300 * attempt);
  }
  await failure;
  assert.equal(h.requests.length, 3);
  assert.equal(h.storage.size, 1);
  assert.equal(h.calls.some(url => url.includes('/complete')), false);
  assert.equal(h.timers.size, 0);
});

test('exhausted retries abort sibling requests and never complete the file', async () => {
  const h = setup();
  const upload = h.start();
  const failure = assert.rejects(upload.promise, /Chunk 0 failed/);
  await h.flush();
  for (let attempt = 1; attempt <= 3; attempt++) {
    h.requests.at(attempt === 1 ? 0 : -1).finish(503);
    await h.flush();
    await h.advance(300 * attempt);
  }
  await failure;
  assert.equal(h.requests[1].aborted, true);
  assert.equal(h.calls.some(url => url.includes('/complete')), false);
  assert.equal(h.storage.size, 1);
  assert.equal(h.timers.size, 0);
});

test('user cancellation clears timers and does not retry', async () => {
  const h = setup();
  const upload = h.start();
  const failure = assert.rejects(upload.promise, /Upload cancelled/);
  await h.flush();
  upload.abort();
  await failure;
  await h.advance(60000);
  assert.equal(h.requests.length, 2);
  assert.equal(h.timers.size, 0);
});

test('repeated progress events without new bytes do not mask a stall', async () => {
  const h = setup(1);
  const upload = h.start();
  const failure = assert.rejects(upload.promise, /Upload cancelled/);
  await h.flush();
  h.requests[0].progress(10);
  await h.advance(45000);
  h.requests[0].progress(10);
  await h.advance(15000);
  assert.equal(h.requests[0].aborted, true);
  upload.abort();
  await h.advance(300);
  await failure;
  assert.equal(h.requests.length, 1);
});

test('a chunk sent fully but never acknowledged also retries', async () => {
  const h = setup(1);
  const upload = h.start();
  await h.flush();
  h.requests[0].progress(100);
  await h.advance(60000);
  assert.equal(h.requests[0].aborted, true);
  assert.equal(h.calls.some(url => url.includes('/complete')), false);
  await h.advance(300);
  h.requests[1].finish();
  await upload.promise;
  assert.equal(h.timers.size, 0);
});
