# Design

The web app follows **Blackletter**: a true black canvas, off-white ink, one blue accent, Geist type, 8px controls and 12px cards, flat surfaces with hairline borders. Tokens live at the top of `app/static/app.css`. The token names (`--brand-green`, `--brand-fill`, …) were kept from the InFocus Drive fork so every rule still maps; their values are below.

The Mac app (`mac/`, `Theme.swift`) still uses the older "Machine & Ink" tokens and has not been moved yet.

## Color

| Role | Dark (default) | Light | Token |
|---|---|---|---|
| Canvas | `#000000` | `#FCFCFD` | `--background`, `--ink`, `--sb-bg` |
| Cards, inputs | `#0A0A0C` | `#FFFFFF` | `--card`, `--muted` |
| Raised surfaces, menus | `#111114` | `#F4F5F7` (`#FFFFFF` for popovers) | `--popover`, `--secondary`, `--sb-surface` |
| Text | `#FCFDFF` | `#0A0A0C` | `--foreground`, `--sb-hd` |
| Secondary text | `#A1A4A5` | `#5F6366` | `--muted-foreground`, `--sb-fg` |
| Hairlines | ~14% white | ~12% black | `--border`, `--sb-br` |
| Accent (links, focus, selection marks, icons) | `#4C8DFF` | `#2F6FE0` | `--brand-green`, tints `--brand-green-a10…a30`, focus `--ring` |
| Primary buttons | `#FCFDFF` fill, black text | `#0A0A0C` fill, `#FCFDFF` text | `--brand-fill`, `--on-brand` |
| Danger | `#FF4D5E` | `#D92D3F` | `--danger`, `--danger-text`, `--on-danger` |
| Warning | `#FFC53D` | `#B7791F` | `--brand-amber` |

Keep blue under a few percent of any screen: links, focus rings, the current folder mark, the wordmark square, small icons. Red, green and amber are state colors only. The media viewer stays dark in both themes (`#06060A` chrome).

## Theme

Dark is the default. The saved choice (`localStorage["ifd-theme"]`) is `system`, `light` or `dark`; with nothing saved the app follows `prefers-color-scheme`. A small script in the `<head>` of `index.html` and `share.html` sets `html.light`, `data-theme` (only for an explicit choice), `color-scheme` and the theme-color meta before first paint. The theme button in the toolbar and on share pages cycles system → light → dark and shows a monitor, sun or moon icon for the current mode.

## Type

- **Geist Serif** (`--font-hero`): the drive name on the sign-in page only, 56–88px, weight 400, line height 1.0, -0.02em. It is not on Google Fonts, so `app.css` loads the vendored `vendor/GeistSerifVF.woff2` (from the `geist` npm package, OFL).
- **Geist** (`--font-sans`, and `--font-display` now points to it too): headings, labels, buttons, forms, UI copy, and data such as sizes, dates and speeds (`tabular-nums`).
- **Geist Mono** (`--font-mono`): code and keys only (the text/code viewer, `kbd`, the Mac sign-in code, share link text).
- No uppercase tracked labels and no weights above 500. Geist and Geist Mono load from Google Fonts in each HTML page.

## Shape and motion

- `--radius` 8px for buttons, inputs, rows and chips; `--radius-lg` 12px for cards, panels, menus, modals and the viewer; full radius for badges, pills, progress bars, avatars, dots and toggles.
- Flat: no drop shadows, gradients, glass or blur. Depth comes from the canvas → card → raised steps and hairlines. Focus is a 3px `--ring`; the current folder keeps its 2px accent bar.
- Hover and focus: 150ms `cubic-bezier(0.2, 0, 0, 1)`. Respect `prefers-reduced-motion`.
- The sign-in headline ends with a static, thin accent caret (it no longer blinks).

## Wordmark

A 12px accent square followed by the drive name in Geist. The name comes from Settings; the server fills it into every `[data-drive-name]` element and the page title.

## Cache-busting

Static assets are cached for a year. Bump the `?v=` query on every changed import in `index.html`, `share.html`, `cli-authorize.html` and the JS modules.
