const { test } = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const vm = require('node:vm');
const source = readFileSync(`${__dirname}/../app/static/api.js`, 'utf8');
const fn = source.match(/^export function uploadChunkSize\([^]*?^}/m)[0].replace('export ', '');
const MiB = 1024 * 1024;
const context = vm.createContext({ UPLOAD_CHUNK_SIZE: 32 * MiB, UPLOAD_CHUNK_STREAMS: 8 });
vm.runInContext(fn, context);

test('mid-size files split across every upload stream', () => {
  assert.equal(context.uploadChunkSize(20 * MiB), 3 * MiB); // 7 chunks, not 1
  assert.equal(context.uploadChunkSize(100 * MiB), 13 * MiB); // 8 chunks, not 4
  assert.equal(context.uploadChunkSize(9 * MiB), 2 * MiB);
});

test('large files keep 32 MiB chunks and tiny ones the 1 MiB minimum', () => {
  assert.equal(context.uploadChunkSize(1024 * MiB), 32 * MiB);
  assert.equal(context.uploadChunkSize(3), MiB);
});
