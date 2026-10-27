# Markdown preview dependencies

Vendored browser ES modules from npm, used by `../markdown.js`:

- `marked` 18.0.13: `lib/marked.esm.js` → `marked.js` (MIT, see `marked.LICENSE`)
- `dompurify` 3.4.15: `dist/purify.es.mjs` → `dompurify.js` (see `dompurify.LICENSE`)

Keep versions pinned when refreshing these files; run the Markdown preview tests after updates.
