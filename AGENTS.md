# AGENTS.md: ugreen-drive

A web file browser for Ugreen NAS (UGOS Pro), shipped as one Docker image, plus a Mac menu-bar app that mounts it in Finder. FastAPI backend, vanilla ES-module frontend, nginx in the same container; SwiftUI app with a bundled Go helper (`ugd`). Architecture: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). Look and tokens: [DESIGN.md](DESIGN.md).

## Public repository

This repo is public and MIT. Every commit, message, branch, issue and PR is world-readable and permanent.

- Never commit secrets, `.env`, `config/` contents, real NAS IPs or hostnames, real people's names, usernames or emails, screenshots of real files, or logs.
- Tests and docs use fictional values: `admin`, `family`, `me@example.com`, `192.168.1.50`, `drive.example.com`.
- Before each commit: read `git diff --cached`, check `git status` for stray files, run `gitleaks git .`.
- Vulnerabilities are reported privately via GitHub Security Advisories.

## Non-negotiables

- File operations go through `fsops` path resolution and run inside `as_user(uid, gid)`. Never touch the share as root.
- Sessions only ever carry NAS users with uid ≥ 1000.
- Keep the existing hardening: session-secret strength check, NAS sign-in lockouts and OTP caps, cross-site write blocking, the inline-preview allowlist and sandbox CSP, symlink checks.
- Google and email sign-in only work for emails an admin linked in Settings → People. Don't add domain-wide or name-matching fallbacks.
- `/api/admin/*` requires a NAS administrator (`shares.is_nas_admin`).
- Setup must stay "paste the compose file, deploy, sign in". New options go in Settings (`runtime_settings.EDITABLE_FIELDS`) with an env default, never as required env.

## Key paths

```
app/main.py              Routes: pages, sign-in, files, uploads, share links, admin settings
app/runtime_settings.py  Settings saved in /config/settings.json; first-run secret; LAN detection
app/config.py            Env defaults (pydantic-settings)
app/users.py             passwd lookups; linked email → NAS user
app/shares.py            Shared-folder discovery (smbshare.conf) and access checks
app/fsops.py             Path-safe file operations as the NAS user
app/ugos_api.py          UGOS login API (password, 2FA) and personal-folder calls
app/email_auth.py        Email-code sign-in (Resend)
app/cli_tokens.py        Mac app / `ugd` sign-ins: PKCE codes and hashed bearer tokens
app/static/              index.html, app.js, api.js, settings.js, viewer.js, player.js, share.*
docker/nginx.conf.template  nginx inside the image; __WEB_PORT__ filled by the entrypoint
scripts/entrypoint.sh    Starts nginx, the app and upload workers; watchdog
scripts/dev_server.py    Local UI preview with sample files and a fake sign-in
docker-compose.yml       What users paste into UGOS → Docker → Project
cli/                     `ugd` (Go): sign-in, file commands, the WebDAV helper Finder mounts
mac/                     Ugreen Drive.app (SwiftUI): build.sh, sign-release.sh; docs/MAC-APP.md
```

## UX rules

| Interaction | Behavior |
|-------------|----------|
| Click folder | Open it |
| Click checkbox | Select |
| Click previewable file | Open the viewer |
| Drag items onto a folder, crumb or tree row | Move (`/api/move`) |
| Drop files from the OS | Upload into the current folder |
| Theme toggle | Main area and sidebar (`--sb-*` tokens) |

## Working on it

- Tests: `.venv/bin/python -m pytest -q`, `node --test tests/*.cjs`, `(cd cli && go test ./...)` and `(cd mac && swift test)`. CI runs all four and builds the multi-arch image to `ghcr.io/neelsatyavolu/ugreen-drive` on pushes to `main` and `v*` tags.
- UI changes: bump the `?v=` cache-bust on every changed asset import in `index.html`, `share.html` and the JS modules.
- Check the UI with `scripts/dev_server.py` (admin / dev) at desktop and phone widths, in both themes.
- Keep changes surgical and match the surrounding style. New frontend features go in their own module rather than growing `app.js`.
