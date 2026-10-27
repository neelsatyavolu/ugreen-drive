# Design

ugreen-drive uses the "Machine & Ink" system: warm ink-black, warm off-white text, one blue accent, flat surfaces and square corners. Tokens live at the top of `app/static/app.css`. The token names (`--brand-green`, `--brand-fill`, …) were kept from the InFocus Drive fork so every rule still maps; their values are below.

## Color

| Role | Dark (default) | Light ("Paper") | Token |
|---|---|---|---|
| Canvas | `#0E0E0D` | `#F7F5EF` | `--background`, `--ink`, `--sb-bg` |
| Text | `#F2F0EA` | `#16161A` | `--foreground`, `--sb-hd` |
| Secondary text | 68% text | 66% text | `--muted-foreground`, `--sb-fg` |
| Hairlines | 10% text | 12% text | `--border`, `--sb-br` |
| Accent (links, focus, selection marks, icons) | `#4C8DFF` | `#2F6FE0` | `--brand-green`, tints `--brand-green-a10…a30` |
| Primary buttons | `#F2F0EA` fill, ink text | `#16161A` fill, paper text | `--brand-fill`, `--on-brand` |
| Danger | `#E5483A` | `#C8352A` | `--danger`, `--danger-text`, `--on-danger` |

Keep blue under a few percent of any screen: the caret, links, focus rings, the current folder mark, small icons. Red and green are state colors only. No pure `#000` or `#FFF` text.

## Type

- **Geist Mono** (`--font-display`, `--font-mono`): headings (often uppercase), labels and eyebrows (12px, uppercase, 0.08em tracking), the wordmark, and data such as sizes, dates and speeds (`tabular-nums`).
- **Geist** (`--font-sans`): buttons, forms and UI copy.
- **Newsreader** (`--font-serif`): prose, such as the sign-in lede.

All three load from Google Fonts in each HTML page.

## Shape and motion

- Radius 0 everywhere (`--radius`). Circles only for avatars, dots, spinners and toggles.
- Flat: no shadows, gradients, glass or blur. Depth comes from hairlines and opacity. 1px rings are fine for focus.
- The current folder is a raised row with a 2px accent bar on the left, not a filled pill.
- Hover and focus: 150ms `cubic-bezier(0.2, 0, 0, 1)`. Respect `prefers-reduced-motion`.
- The one living element is the blinking accent caret after the name on the sign-in page.

## Wordmark

A 12px accent square followed by the drive name in Geist Mono caps. The name comes from Settings; the server fills it into every `[data-drive-name]` element and the page title.

## Cache-busting

Static assets are cached for a year. Bump the `?v=` query on every changed import in `index.html`, `share.html` and the JS modules.
