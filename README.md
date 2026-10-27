# ugreen-drive

A fast web file browser for Ugreen NAS (UGOS Pro). Sign in with your NAS account and browse, upload, preview and share your shared folders from any browser, with the same permissions you have in UGOS.

It's a fork of [InFocus Drive](https://github.com/neelsatyavolu/infocus-drive), which runs in production for a student media program, with the school-specific parts taken out and setup reduced to pasting one file into the UGOS Docker app.

**Stack:** FastAPI (Python 3.12) · vanilla ES modules (no framework, no build step) · nginx · one Docker image

## Features

- **NAS sign-in.** Use your UGOS username and password, including UGOS two-factor codes. Every file operation runs as that NAS user, so UGOS permissions stay in charge.
- **More ways to sign in (optional).** Admins can turn on Google sign-in or emailed codes in Settings, and link each email to a NAS account.
- **File manager.** List and grid views, a folder tree, drag-and-drop moves, rename, new folder, delete to the shared folder's Recycle Bin, search, and folders downloaded as ZIP.
- **Big uploads.** Chunked, parallel and resumable, with several upload worker processes so one CPU core isn't the limit. Folder uploads can replace or merge, and merge skips files that haven't changed.
- **Previews.** Images, video and audio (with seeking), PDF, text and Markdown. Video thumbnails come from ffmpeg.
- **Share links.** Expiring public links for a file or a whole folder.
- **LAN fast path.** When your browser can reach the NAS directly, the drive switches to its LAN address and skips the tunnel.
- **Settings in the browser.** Name, public address, sign-in methods and linked people are set from the web UI, not config files.

## Set it up

You need a Ugreen NAS on UGOS Pro with the Docker app installed (App Center → Docker), and an admin account.

1. In UGOS, open **Docker → Project → Create**. Name it `ugreen-drive` and pick a folder such as `/volume1/docker/ugreen-drive`.
2. Paste [`docker-compose.yml`](docker-compose.yml) from this repo. If the NAS has more than one storage volume, add a line for each one (`- /volume2:/data/volume2`).
3. Click **Deploy**. The first start downloads the image.
4. Open `http://<NAS-IP>:8790` and sign in with your UGOS account.

That's all for use at home. The first start creates its own secrets in the project's `config` folder, so there's nothing to fill in.

### Use it away from home (Cloudflare Tunnel)

1. In the [Cloudflare Zero Trust dashboard](https://one.dash.cloudflare.com/), go to **Networks → Tunnels → Create a tunnel** (type Cloudflared). Copy the token it shows.
2. Add a public hostname, for example `drive.example.com`, with service `http://localhost:8790`.
3. In the compose file, uncomment the `tunnel` service, paste the token, and redeploy the project.
4. In the drive, open **Settings** and set **Public address** to `https://drive.example.com`.

Browsers on your home network still switch to the LAN address on their own.

### Google sign-in or email codes (optional)

Open **Settings** (shown to NAS administrators).

- **Google:** create an OAuth web client in Google Cloud (APIs & Services → Credentials). Use the redirect URI shown in Settings, which is `<public address>/auth/callback`. Paste the client ID and secret.
- **Email codes:** add a [Resend](https://resend.com) API key and a sender on a verified domain.
- **People:** link each email to a NAS account. Only linked emails can sign in this way. To add a person who has no NAS account, create one in UGOS → Control Panel → User first.

### Updating

In UGOS, open **Docker → Project → ugreen-drive** and pull the latest image (or redeploy). Settings and sign-in state stay in the `config` folder.

### Optional environment variables

Everything can be set in the web UI, but env values work as defaults: `PUBLIC_BASE_URL`, `DRIVE_NAME`, `LAN_ORIGIN`, `WEB_PORT` (default 8790), `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `RESEND_API_KEY`, `RESEND_FROM_EMAIL`, `SESSION_SECRET`. See [`.env.example`](.env.example).

## How it works

```
browser ──► (Cloudflare Tunnel) ──► nginx :8790 ──► FastAPI 127.0.0.1:8787 ──► /volumeN (as the signed-in NAS user)
                                         │                  │
                                         └► upload workers  └► UGOS API 127.0.0.1:9999 (password + 2FA check)
```

The container runs as root with only `SETUID`/`SETGID` so it can switch to the signed-in NAS user for each file operation. It reads the NAS's accounts and shared-folder list read-only from `/etc`. More detail is in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -r app/requirements.txt pytest
.venv/bin/python -m pytest -q          # backend tests
node --test tests/*.cjs                # frontend tests (markdown test needs jsdom)
.venv/bin/python scripts/dev_server.py # local UI on http://127.0.0.1:8787, admin / dev
```

The dev server uses sample files in a temp folder and a fake sign-in. Never run it on a NAS.

Contributor and coding-agent notes are in [AGENTS.md](AGENTS.md). The look follows [DESIGN.md](DESIGN.md).

## Security

Report vulnerabilities privately through GitHub Security Advisories, not public issues.

## License

MIT. See [LICENSE](LICENSE).
