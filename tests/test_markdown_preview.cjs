const { test } = require('node:test');
const assert = require('node:assert/strict');
const { pathToFileURL } = require('node:url');

// Test setup: npm install --prefix /tmp/ugd-markdown-check --ignore-scripts jsdom@30.1.0
// Run: NODE_PATH=/tmp/ugd-markdown-check/node_modules node --test tests/test_markdown_preview.cjs
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<!doctype html>', { url: 'https://drive.example/' });
global.window = dom.window;
global.document = dom.window.document;
const moduleUrl = pathToFileURL(require('node:path').resolve(__dirname, '../app/static/markdown.js'));

async function setup(text) {
  const { enhanceMarkdownPreview } = await import(moduleUrl);
  const container = document.createElement('div');
  const raw = document.createElement('pre');
  raw.textContent = text;
  const bar = document.createElement('div');
  const wrapButton = document.createElement('button');
  bar.append(wrapButton);
  container.append(raw, bar);
  enhanceMarkdownPreview({ container, raw, bar, text, wrapButton });
  return { container, raw, bar, wrapButton };
}

test('renders Markdown, toggles exact raw source, and restores preview', async () => {
  const text = '# Heading\n\n**Bold** and `code`\n\n| Setting | Value |\n| --- | --- |\n| GPU | Ultra |\n\n- [x] Done\n\n```js\nconst x = 1;\n```';
  const { container, raw, bar, wrapButton } = await setup(text);
  assert.equal(container.querySelector('h1').textContent, 'Heading');
  assert.equal(container.querySelector('td').textContent, 'GPU');
  assert.equal(container.querySelector('input').disabled, true);
  assert.equal(raw.hidden, true);
  assert.equal(wrapButton.hidden, true);
  const buttons = bar.querySelectorAll('.markdown__modes button');
  buttons[1].click();
  assert.equal(raw.hidden, false);
  assert.equal(raw.textContent, text);
  assert.equal(wrapButton.hidden, false);
  assert.equal(buttons[1].getAttribute('aria-pressed'), 'true');
  buttons[0].click();
  assert.equal(raw.hidden, true);
  assert.equal(container.querySelectorAll('h1').length, 1);
});

test('removes executable HTML, unsafe URLs and document styling', async () => {
  const { container } = await setup('<script>alert(1)</script>\n\n<img src=x onerror="alert(1)">\n\n[bad](javascript:alert%281%29)\n\n<style>body{display:none}</style><iframe src="https://example.com"></iframe>\n\n<a href="https://example.com" style="position:fixed" id="app">safe</a>');
  const preview = container.querySelector('.markdown');
  assert.equal(preview.querySelector('script,style,iframe,[onerror],[style],[id]'), null);
  assert.equal(preview.querySelector('a[href^="javascript:"]'), null);
  assert.equal(preview.querySelector('a[href="https://example.com"]').rel, 'noopener noreferrer');
});

test('recognizes only Markdown extensions, case insensitively', async () => {
  const { isMarkdown } = await import(moduleUrl);
  assert.equal(isMarkdown('NOTES.MD'), true);
  assert.equal(isMarkdown('notes.markdown'), true);
  assert.equal(isMarkdown('notes.txt'), false);
  assert.equal(isMarkdown('notes.md.txt'), false);
});
