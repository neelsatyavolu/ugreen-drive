# Architecture

## One container

`ugreen-drive` is a single image (`Dockerfile`) that runs, on the NAS's host network:

| Process | Listens on | Job |
|---|---|---|
| nginx | `0.0.0.0:$WEB_PORT` (8790) | The only network port. Streams uploads and downloads without buffering, gzips JSON, caches `/assets/`. No access log (query strings carry tokens). |
| uvicorn main app | `/run/ugreen-drive/app.sock` | Everything: pages, API, sign-in, background jobs. |
| uvicorn upload workers | `/run/ugreen-drive/upload-{0,1,2}.sock` | Only chunk bodies (`/api/upload/chunk`, `/api/speedtest/upload`). One Python process tops out near one CPU core; nginx spreads chunks over these with the main app as fallback. |

The sockets are inside the container (group `www-data`, mode 0770), so other host-network containers can't take or reach the app's port. nginx sets `X-Forwarded-For` and `X-Forwarded-Proto` itself and only passes Cloudflare's client IP and `https` through when the connection comes from cloudflared on 127.0.0.1, so the app can trust those headers. `scripts/entrypoint.sh` starts all of them and runs a watchdog that exits (so Docker restarts the container) if `/api/health` stops answering, and restarts a dead worker or nginx.

Host network is needed so the app can reach the UGOS API on `127.0.0.1:9999`, and so the LAN address `http://<NAS-IP>:8790` works without port mapping.

## Mounts

| Container path | Host | Why |
|---|---|---|
| `/data/volumeN` | `/volumeN` | The shared folders. |
| `/data/home` | `/home` (rslave) | UGOS personal folders, including encrypted ones after unlock. |
| `/etc/passwd`, `/etc/group` | same, read-only | uid/gid and group lookups for `seteuid`. |
| `/host-etc` | `/etc`, read-only | Fresh reads of `passwd`/`group` (file binds go stale after UGOS rewrites them) and `samba/smbshare.conf`, the shared-folder list. |
| `/config` | `./config` next to the compose file | `settings.json`, `session_secret`, the email-code database. |

## Sign-in

- **NAS password** (`POST /auth/nas`, always on): the UGOS API checks the password, and starts a 2FA challenge if the account has one (`POST /auth/nas/otp`). Rate limits per IP, a 15-minute lockout after 5 failures per username, and 5 codes per challenge.
- **Google** (`/auth/login` → `/auth/callback`) and **email codes** (`/auth/email/*`, Resend): on when configured in Settings. The email must be linked to a NAS user in Settings → People (`users.resolve_nas_user`). There is no domain-wide or name-matching fallback.
- Sessions are signed cookies (5 days). Every session holds a NAS username, uid and gid; uid must be at least 1000. Google and email sessions also record how they signed in, and are checked against Settings → People on every request, so unlinking an email signs it out at once.
- UGOS replies only count when they say `code: 200` and return a session token; anything else is a failed sign-in or 2FA code.
- **Mac app and `ugd`** (`/cli/authorize` → `POST /api/cli/token`): a PKCE sign-in approved in the browser from a cookie session, then `Authorization: Bearer ugd_…` on every call (`app/cli_tokens.py`; details in [MAC-APP.md](MAC-APP.md)). Bearer requests skip the cookie cross-site check and never set the share cookie. Admin routes, `/api/share` and `/api/lan-handoff` accept only the cookie.
- **LAN hand-off:** Google always returns to the public address. If sign-in started on the LAN address, the callback mints a 90-second signed token and redirects to `/auth/lan-handoff` on the LAN origin, which sets the cookie there.

## Settings

`app/runtime_settings.py` overlays `/config/settings.json` onto the env-based `Settings` at import and after each save. Empty saved values fall back to env, then to detection (LAN IP from the default route, public address = LAN address). Secrets are write-only from the UI. `GET/PUT /api/admin/settings` require a NAS administrator (Linux group `admin`, which UGOS uses).

The session secret comes from `SESSION_SECRET` if it is at least 32 characters, else from `/config/session_secret`, created on first start with a hard link so the main app and the upload workers agree on one value.

## Files

`app/fsops.py` resolves every path under the active shared folder (no `..`, no symlinks out of the share) and runs each operation inside `as_user(uid, gid)`, which switches effective ids and supplementary groups. `app/shares.py` builds the list of shared folders from `smbshare.conf` (falling back to the volume directories), filtered by Samba `valid users` and a real directory listing as the user.

Uploads of 8 MiB or more use `upload/init` → parallel `upload/chunk` (up to 32 MiB each) → `upload/complete`, staged in `/tmp` and renamed into place. The browser keeps the upload id to resume after a dropped connection.

Downloads honor `Range`. Folder and multi-file downloads stream an uncompressed ZIP (up to 10,000 files / 8 GiB). Inline previews are limited to safe types and get a sandboxing CSP.

## Share links

`app/file_links.py` signs `{share, path, expiry}` with the session secret. `/s/<token>` is a standalone page (`share.html`) that can preview, list a folder and download, without signing in.

## Frontend

Plain ES modules in `app/static/`, no build step. `app.js` is the file browser, `api.js` the HTTP client, `settings.js` the admin Settings modal, `viewer.js`/`player.js` previews, `share.js` the share-link page. Bump the `?v=` query on every changed asset import (they are cached for a year).
