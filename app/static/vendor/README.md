# Vendored dependencies

## Markdown preview

Vendored browser ES modules from npm, used by `../markdown.js`:

- `marked` 18.0.13: `lib/marked.esm.js` → `marked.js` (MIT, see `marked.LICENSE`)
- `dompurify` 3.4.15: `dist/purify.es.mjs` → `dompurify.js` (see `dompurify.LICENSE`)

Keep versions pinned when refreshing these files; run the Markdown preview tests after updates.

## Font

- Geist Serif (`geist` npm package 1.9.0): `dist/fonts/geist-serif/GeistSerifVF.woff2` → `GeistSerifVF.woff2` (SIL OFL 1.1, see `geist.LICENSE`). Used by `app.css` for the sign-in headline; it is not on Google Fonts.
