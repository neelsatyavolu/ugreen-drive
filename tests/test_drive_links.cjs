const { test } = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const vm = require('node:vm');

const source = readFileSync(new URL('../app/static/app.js', `file://${__filename}`), 'utf8');
function setup() {
  const calls = { loads: [], previews: [], messages: [], login: 0 };
  const context = vm.createContext({
    URL, URLSearchParams, Set, location: { hash: '#/', search: '', origin: 'https://drive.example.com' },
    state: { me: { authenticated: true }, share: 'Drive',
      shares: [{ id: 'Drive' }, { id: 'Photos' }], items: [], selection: new Set() },
    SHARE_KEY: 'share', loadRequestId: 0,
    pathParts: path => path.split('/').filter(Boolean),
    parentPath: path => path.split('/').slice(0, -1).join('/'),
    navigator: { clipboard: { writeText: async text => { calls.clipboard = text; } } },
    ApiError: class extends Error { constructor(message, status) { super(message); this.status = status; } },
    api: { setShare: async share => ({ share }), setActiveShare: share => { calls.share = share; } },
    loadFolder: async path => { calls.loads.push(path); },
    showLogin: () => { calls.login++; },
    toast: message => calls.messages.push(message),
    openPreview: item => calls.previews.push(item.path),
    previewKind: item => item.path.endsWith('.pdf'),
    visibleItems: () => context.state.items,
    store() {}, setQuickScope() {}, clearSearch() {}, loadUsage() {}, refreshShortcuts() {},
    render() {}, scrollItemIntoView() {},
  });
  for (const name of ['copyItemLinks', 'openLinkHash', 'pathFromHash', 'hashForPath', 'loadRoute']) {
    const body = source.match(new RegExp(`^(?:async )?function ${name}\\([^]*?^}`, 'm'));
    assert.ok(body, `Missing ${name}`);
    vm.runInContext(body[0], context);
  }
  return { context, calls };
}

test('folder and file links round-trip reserved characters using the public host', async () => {
  const { context: c, calls } = setup();
  const folder = 'A & B/100% #? café';
  const file = `${folder}/cut + final?.pdf`;
  await c.copyItemLinks([{ path: folder, is_dir: true }, { path: file, is_dir: false }]);
  const links = calls.clipboard.split('\n').map(link => new URL(link));
  assert.equal(links.length, 2);
  for (const url of links) {
    // The path is in the query (not the hash) so link previews can see it.
    assert.equal(url.origin + url.pathname, 'https://drive.example.com/open');
    assert.equal(url.hash, '');
    c.location.hash = c.openLinkHash(url.search);
    assert.equal(c.pathFromHash(), folder);
    assert.equal(new URLSearchParams(c.location.hash.split('?')[1]).get('share'), 'Drive');
  }
  assert.equal(links[0].searchParams.get('path'), folder);
  assert.equal(links[1].searchParams.get('file'), file);
  assert.equal(new URLSearchParams(c.openLinkHash(links[1].search).split('?')[1]).get('file'), file);
});

test('open links without a share or path land on the drive root', () => {
  const { context: c } = setup();
  assert.equal(c.openLinkHash(''), '#/');
  assert.equal(c.openLinkHash('?share=Photos'), '#/?share=Photos');
});

test('signed-out recipients must sign in before listing', async () => {
  const { context: c, calls } = setup();
  c.state.me = null;
  await c.loadRoute();
  assert.equal(calls.login, 1);
  assert.equal(calls.loads.length, 0);
});

test('linked share overrides saved share before folder loading', async () => {
  const { context: c, calls } = setup();
  c.location.hash = '#/Album?share=Photos';
  await c.loadRoute();
  assert.equal(c.state.share, 'Photos');
  assert.equal(calls.share, 'Photos');
  assert.deepEqual(calls.loads, ['Album']);
});

test('unavailable share and server fallback never list the wrong drive', async () => {
  for (const share of ['Private', 'Photos']) {
    const { context: c, calls } = setup();
    c.location.hash = `#/Album?share=${share}`;
    c.api.setShare = async () => ({ share: 'Drive' });
    await c.loadRoute();
    assert.equal(c.state.error.status, 403);
    assert.equal(calls.loads.length, 0);
  }
});

test('file links select the file and preview only supported types', async () => {
  for (const filename of ['doc.pdf', 'archive.zip']) {
    const { context: c, calls } = setup();
    c.location.hash = `#/?share=Drive&file=${filename}`;
    c.state.items = [{ path: filename, is_dir: false }];
    await c.loadRoute();
    assert.ok(c.state.selection.has(filename));
    assert.equal(calls.previews.length, filename.endsWith('.pdf') ? 1 : 0);
  }
});

test('missing files show a message without opening another file', async () => {
  const { context: c, calls } = setup();
  c.location.hash = '#/?file=missing.pdf';
  await c.loadRoute();
  assert.match(calls.messages[0], /not found/);
  assert.equal(calls.previews.length, 0);
});

test('Google and email login preserve the linked destination', () => {
  const { context: c } = setup();
  const link = {};
  c.document = { querySelectorAll: () => [link] };
  let bound = false;
  c.bindEmailSignIn = () => { bound = true; };
  c.$ = () => ({});
  c.show = c.hideBootSplash = c.bindNasLogin = () => {};
  c.location.hash = '#/Album?share=Photos&file=Album%2Fdoc.pdf';
  vm.runInContext(source.match(/^function showLogin\([^]*?^}/m)[0], c);
  c.showLogin();
  assert.equal(bound, true);
  assert.equal(new URL(link.href, 'https://drive.example.com').searchParams.get('next'), `/${c.location.hash}`);
  assert.equal(c.location.hash, '#/Album?share=Photos&file=Album%2Fdoc.pdf');
});

function fallbackClipboard(c, succeeds = true) {
  const calls = { copied: [], prompts: [], removed: 0 };
  let field;
  c.document = {
    activeElement: { focus() {} },
    createElement: () => ({ style: {}, setAttribute() {}, focus() {}, select() {},
      remove() { calls.removed++; } }),
    body: { appendChild(node) { field = node; } },
    execCommand(command) {
      assert.equal(command, 'copy');
      calls.copied.push(field.value);
      return succeeds;
    },
  };
  c.window = { prompt: (...args) => calls.prompts.push(args) };
  return calls;
}

test('copy link works when the modern clipboard API is unavailable on LAN', async () => {
  const { context: c, calls } = setup();
  delete c.navigator.clipboard;
  const fallback = fallbackClipboard(c);
  await c.copyItemLinks([{ path: 'Folder', is_dir: true }]);
  assert.equal(fallback.copied.length, 1);
  assert.match(fallback.copied[0], /\/open\?share=Drive&path=Folder$/);
  assert.equal(fallback.removed, 1);
  assert.match(calls.messages[0], /Link copied/);
});

test('copy link falls back when clipboard permission is denied', async () => {
  const { context: c } = setup();
  c.navigator.clipboard.writeText = async () => { throw new Error('NotAllowedError'); };
  const fallback = fallbackClipboard(c);
  await c.copyItemLinks([{ path: 'doc.pdf', is_dir: false }]);
  assert.equal(fallback.copied.length, 1);
  assert.equal(fallback.prompts.length, 0);
});

test('both clipboard methods failing still exposes the links for manual copy', async () => {
  for (const throws of [false, true]) {
    const { context: c, calls } = setup();
    delete c.navigator.clipboard;
    const fallback = fallbackClipboard(c, false);
    if (throws) c.document.execCommand = () => { throw new Error('Blocked'); };
    await c.copyItemLinks([{ path: 'A', is_dir: true }, { path: 'B', is_dir: true }]);
    assert.equal(fallback.prompts.length, 1);
    assert.equal(fallback.prompts[0][1].split('\n').length, 2);
    assert.equal(fallback.removed, 1);
    assert.ok(!calls.messages.some(message => /Links? copied/.test(message)));
  }
});
