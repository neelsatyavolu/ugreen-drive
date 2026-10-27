const { test } = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const vm = require('node:vm');
const source = readFileSync(`${__dirname}/../app/static/app.js`, 'utf8');

function setup({ items = [], choices = [], uploads = [], deleteError } = {}) {
  const calls = [];
  const context = vm.createContext({
    state: { share: 'drive', uploads },
    api: {
      listFiles: async path => { calls.push(['list', path]); return { items }; },
      deleteItem: async path => { calls.push(['delete', path]); if (deleteError) throw deleteError; },
    },
    promptFolderUploadConflict: async name => { calls.push(['prompt', name]); return choices.shift(); },
  });
  for (const name of ['joinRelPath', 'prepareFolderUploads']) {
    const match = source.match(new RegExp(`^(?:async )?function ${name}\\([^]*?^}`, 'm'));
    assert.ok(match, `${name} is implemented`);
    vm.runInContext(match[0], context);
  }
  return { context, calls, run: names => context.prepareFolderUploads(names.map(name => ({ name })), 'parent', 'drive') };
}
const folder = name => ({ name, path: `parent/${name}`, is_dir: true });

test('plain files bypass folder checks; new folders upload without prompting', async () => {
  const h = setup();
  assert.equal(await h.run(['a.txt']), true);
  assert.deepEqual(h.calls, []);
  assert.equal(await h.run(['New/a.txt']), true);
  assert.deepEqual(h.calls, [['list', 'parent']]);
});
test('merge prompts once per root and preserves existing folder', async () => {
  const h = setup({ items: [folder('Photos')], choices: ['merge'] });
  assert.equal(await h.run(['Photos/a.jpg', 'Photos/nested/b.jpg']), true);
  assert.deepEqual(h.calls, [['list', 'parent'], ['prompt', 'Photos']]);
});
test('replace removes only the selected conflicting root before upload', async () => {
  const h = setup({ items: [folder('Photos'), folder('Other')], choices: ['replace'] });
  assert.equal(await h.run(['Photos/a.jpg']), true);
  assert.deepEqual(h.calls, [['list', 'parent'], ['prompt', 'Photos'], ['delete', 'parent/Photos']]);
});
test('cancel aborts the batch before any replacement', async () => {
  const h = setup({ items: [folder('A'), folder('B')], choices: ['replace', null] });
  assert.equal(await h.run(['A/a', 'B/b']), false);
  assert.equal(h.calls.some(c => c[0] === 'delete'), false);
});
test('failed replacement prevents uploads', async () => {
  const h = setup({ items: [folder('A')], choices: ['replace'], deleteError: new Error('Permission denied') });
  await assert.rejects(h.run(['A/a']), /Permission denied/);
});
test('share change while prompt is open cancels without deleting', async () => {
  const h = setup({ items: [folder('A')] });
  h.context.promptFolderUploadConflict = async () => { h.context.state.share = 'other'; return 'replace'; };
  assert.equal(await h.run(['A/a']), false);
  assert.equal(h.calls.some(c => c[0] === 'delete'), false);
});
test('replacement cannot remove a folder receiving an active upload', async () => {
  const h = setup({ items: [folder('A')], choices: ['replace'], uploads: [{ status: 'uploading', targetPath: 'parent/A/nested' }] });
  await assert.rejects(h.run(['A/a']), /upload/i);
  assert.equal(h.calls.some(c => c[0] === 'delete'), false);
});

test('merge workers skip identical files and upload changed files', async () => {
  for (const unchanged of [true, false]) {
    const sent = [];
    const h = setup({ items: [folder('Photos')], choices: ['merge'] });
    Object.assign(h.context, {
      describeKind: () => ({}), renderUploads() {}, scheduleListingRefresh() {},
      loadFolder: async () => {}, clearTimeout() {}, handleMutationError: error => { throw error; },
      destinationInView: () => false, AbortController,
    });
    h.context.state.path = 'parent';
    h.context.api.uploadFileUnchanged = async (path) => {
      assert.equal(path, 'parent/Photos/a.jpg');
      return unchanged;
    };
    h.context.api.uploadFile = (path, file) => {
      sent.push(file.name);
      return { promise: Promise.resolve(), abort() {} };
    };
    vm.runInContext('let uploadPreparation = Promise.resolve();\n' + source.match(/^async function startUploads\([^]*?^}/m)[0], h.context);
    await h.context.startUploads([{ name: 'a.jpg', webkitRelativePath: 'Photos/a.jpg', size: 10 }]);
    assert.equal(sent.length, unchanged ? 0 : 1);
    assert.equal(h.context.state.uploads[0].status, 'done');
    assert.equal(h.context.state.uploads[0].skipped, unchanged);
  }
});

test('conflict modal buttons and dismissal resolve the selected choice', async () => {
  for (const choice of ['Merge', 'Replace', 'Cancel', 'dismiss']) {
    let modal, onClose;
    const context = vm.createContext({
      el: (tag, props, children = []) => ({ tag, ...props, children }),
      modalFooter: children => ({ children }),
      openModal: (node, opts) => { modal = node; onClose = opts.onClose; },
      closeModal: () => onClose(),
    });
    vm.runInContext(source.match(/^function promptFolderUploadConflict\([^]*?^}/m)[0], context);
    const pending = context.promptFolderUploadConflict('<Photos>');
    const buttons = modal.children[0].children.at(-1).children;
    if (choice === 'dismiss') onClose();
    else buttons.find(b => b.text === choice).onclick();
    assert.equal(await pending, ['Cancel', 'dismiss'].includes(choice) ? null : choice.toLowerCase());
  }
});
