const { test } = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const vm = require('node:vm');

const source = readFileSync(`${__dirname}/../app/static/app.js`, 'utf8');
function render(uploads, downloads = [], now = 0) {
  const nodes = new Map();
  const make = (tag, props = {}, children = []) => ({
    tag, ...props, children, style: {}, setAttribute() {},
    append(child) { this.children.push(child); },
  });
  const context = vm.createContext({
    state: { uploads, downloads },
    $: id => { if (!nodes.has(id)) nodes.set(id, make('div')); return nodes.get(id); },
    el: make, icon: () => make('svg'), show() {},
    formatSize: n => `${n} bytes`, formatSpeed: n => n ? `${n} B/s` : null,
    formatEta: () => 'a few seconds', performance: { now: () => now },
  });
  for (const name of ['allTransfers', 'isTransferActive', 'isUploadProcessing', 'transferSweepDelay',
    'transferRemainingBytes', 'transferLoadedBytes', 'renderUploads']) {
    const match = source.match(new RegExp(`^function ${name}\\([^]*?^}`, 'm'));
    if (match) vm.runInContext(match[0], context);
  }
  context.renderUploads();
  return { nodes, context, rows: JSON.stringify(nodes.get('upload-list')) };
}
const upload = overrides => ({ name: 'large.bin', direction: 'upload', status: 'uploading',
  size: 100, loadedBytes: 100, progress: 1, speedBps: 20, ...overrides });

test('sent upload shows processing until server confirms completion', () => {
  const entry = upload();
  const { nodes, context, rows } = render([entry]);
  assert.equal(nodes.get('upload-title').textContent, 'Processing…');
  assert.match(rows, /Upload sent · saving file…/);
  assert.doesNotMatch(rows, /B\/s|seconds left/);
  assert.doesNotMatch(nodes.get('upload-foot').textContent, /B\/s/);
  assert.match(nodes.get('upload-foot').textContent, /1 processing/);
  assert.equal(context.isTransferActive(entry), true);
  assert.equal(context.transferLoadedBytes(entry), 100);
});

test('mixed batch retains transfer speed only for files still sending', () => {
  const { nodes, rows } = render([upload(), upload({ progress: 0.5, loadedBytes: 50, speedBps: 10 })]);
  assert.match(nodes.get('upload-title').textContent, /Uploading 2 files/);
  assert.match(nodes.get('upload-foot').textContent, /10 B\/s/);
  assert.match(nodes.get('upload-foot').textContent, /1 processing/);
  assert.match(rows, /saving file/);
});

test('retry progress returns to uploading and completion clears processing', () => {
  for (const [overrides, expected] of [
    [{ progress: 0.4, loadedBytes: 40 }, /Uploading/],
    [{ status: 'done' }, /Upload complete/],
    [{ status: 'error', error: 'Disk full' }, /Uploaded 0 of 1/],
  ]) {
    const { nodes, rows } = render([upload(overrides)]);
    assert.match(nodes.get('upload-title').textContent, expected);
    assert.doesNotMatch(rows, /saving file/);
  }
});

test('downloads at 100 percent do not use upload processing copy', () => {
  const { nodes, rows } = render([], [upload({ direction: 'download', status: 'downloading' })]);
  assert.match(nodes.get('upload-title').textContent, /Downloading/);
  assert.doesNotMatch(rows, /saving file/);
});

test('merge distinguishes checking from skipped unchanged files', () => {
  const checking = render([upload({ progress: 0, loadedBytes: 0, speedBps: null, checking: true })]);
  assert.match(checking.rows, /is-indeterminate/);
  assert.match(checking.rows, /Comparing with existing file/);
  const skipped = render([upload({ status: 'done', skipped: true })]);
  assert.match(skipped.rows, /Skipped/);
  assert.match(skipped.rows, /Unchanged · already exists/);
  assert.match(skipped.nodes.get('upload-foot').textContent, /1 unchanged · skipped/);
});

test('indeterminate row bar keeps its sweep phase across re-renders', () => {
  const bar = rows => JSON.parse(rows).children[0].children[1].children[0];
  const first = bar(render([upload()], [], 1500).rows);
  assert.match(first.class, /is-indeterminate/);
  assert.equal(first.style.animationDelay, '-300ms');
  assert.equal(bar(render([upload()], [], 2600).rows).style.animationDelay, '-200ms');
  assert.equal(bar(render([upload({ progress: 0.5 })], [], 1500).rows).style.animationDelay, undefined);
});
