const { test } = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { createHash, webcrypto } = require('node:crypto');
const vm = require('node:vm');
const source = readFileSync(`${__dirname}/../app/static/api.js`, 'utf8');
const fn = source.match(/^export async function uploadFileUnchanged\([^]*?^}/m)[0].replace('export ', '');
function fingerprint(bytes) {
  const hash = createHash('sha256');
  for (let i = 0; i < bytes.length; i += 8 * 1024 * 1024) {
    hash.update(createHash('sha256').update(bytes.subarray(i, i + 8 * 1024 * 1024)).digest());
  }
  return hash.digest('hex');
}
for (const size of [0, 5, 8 * 1024 * 1024 + 1]) {
  test(`content comparison skips identical ${size}-byte files and catches changes`, async () => {
    const bytes = Buffer.alloc(size, 97);
    const context = vm.createContext({ crypto: webcrypto, Uint8Array, form: x => x,
      request: async () => ({ fingerprint: fingerprint(bytes) }) });
    vm.runInContext(fn, context);
    assert.equal(await context.uploadFileUnchanged('Photos/a', new Blob([bytes])), true);
    assert.equal(await context.uploadFileUnchanged('Photos/a', new Blob([Buffer.alloc(size || 1, 98)])), false);
  });
}
test('new or different-size files do not need browser hashing', async () => {
  const context = vm.createContext({ form: x => x, request: async () => ({ fingerprint: null }) });
  vm.runInContext(fn, context);
  assert.equal(await context.uploadFileUnchanged('new/a', {}), false);
});
