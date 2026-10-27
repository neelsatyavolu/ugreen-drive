# The Mac app

A menu-bar app that puts the drive in Finder as a normal volume (`/Volumes/<drive name>`, listed under **Locations**) and keeps it mounted, at home and away. It signs in with your NAS account through the drive's own web page, so it needs no SMB, no extra port and no NAS password stored on the Mac.

## Install

In the drive, open **Connect in Finder** in the sidebar and paste the install line into Terminal:

```sh
curl -fsSL https://drive.example.com/mac/install.sh | sh
```

The installer (`app/static/mac/install.sh`, served at `/mac/install.sh` with the drive's public address filled in) downloads `Ugreen-Drive-mac.zip` from the latest GitHub release, checks it against `SHA256SUMS`, installs `Ugreen Drive.app` to `/Applications` (or `~/Applications` without admin rights), pre-fills the drive's address and opens the app. Run it again to update. You can also download the zip from the releases page and type the address in the app. Needs macOS 13 or later.

## Use it

1. Click **Sign in**. Your browser opens the drive's approval page: sign in with your NAS account if asked, check that the code matches the one in the app, and click **Allow**.
2. The drive mounts. It remounts by itself on launch, after wake and when the network comes back. **Eject** in Finder (or **Disconnect**) stops it until you click **Connect**.
3. **Start at login** keeps it mounted after a restart (a LaunchAgent starts the app with `--background`).
4. **Updates are automatic.** Every hour the app checks the latest `vX.Y.Z` release. A new version downloads `Ugreen-Drive-mac.zip`, checks its SHA-256, a Developer ID signature from the same team as the running app and notarization, swaps the app and relaunches. It never restarts while Finder is copying.
5. **Keep Drive connected after Quit** (on by default): Quit closes the window and the drive stays in Finder. **Quit Completely** unmounts and exits.

The menu shows uploads with progress, a status grid (account, drive latency, Finder volume, helper, network, start at login) and your shared folders. **Help** has troubleshooting and **Copy diagnostics** (no tokens or passwords).

An encrypted personal folder shows a lock under **Shares**; click it to unlock with the UGOS encryption password or key file, as on the website. Unlocking with a NAS sign-in needs the drive's HTTPS address.

## How it works

```
Finder ──WebDAV──► 127.0.0.1:PORT  ugd webdav ──HTTPS + bearer──► the drive's /api
```

- The app bundles `ugd` (`cli/`, Go). **Sign in** runs `ugd login`: a PKCE flow through `/cli/authorize` in the browser, with the one-time code handed to a listener on 127.0.0.1 only through a 303 redirect. The token is kept in the login Keychain (service `ugreen-drive`).
- **Connect** starts `ugd webdav` on loopback with a fresh random password (passed on stdin, never in argv) and mounts it with Apple's WebDAV client (`NetFSMountURLAsync`). No FUSE, no File Provider.
- On the home network the helper proves the LAN address is this drive (`/api/lan/proof`, an HMAC with the session secret) and switches to it with an 8-hour LAN-only token, skipping the tunnel.
- The helper reports uploads, open writes and latency as JSON lines on stdout, which the app shows.

Server side (`app/cli_tokens.py`, routes in `app/main.py`): tokens are stored only as SHA-256 hashes in `/config/cli_tokens.sqlite3`, expire after 30 idle days or 90 days, and are checked against `/etc/passwd` on every request, so a removed NAS user loses access. A token approved from a Google or email-code session stops working as soon as that email is unlinked in Settings → People. Admin settings, approving an app and the browser LAN hand-off stay cookie-only.

The drive's address may be `https://…`, or `http://…` only on the home network (localhost, `.local`, private and link-local IPs), so it works before a tunnel is set up.

## Limits

- A file you save is written to a temp file on the Mac and uploaded when Finder closes it, so a very large copy needs that much free disk space.
- Moving items between shared folders isn't supported; copy, then delete.
- Changes made elsewhere show up within a few seconds of Finder next looking.

## Build

Needs Xcode (Swift 5.9+) and Go.

```sh
VERSION=0.1.0 mac/build.sh   # mac/build/Ugreen Drive.app + Ugreen-Drive-mac.zip (universal, ad-hoc signed)
(cd mac && swift test)
```

`DRIVE_URL=https://drive.example.com` bakes in a default address. `CONFIGURATION=debug mac/build.sh` builds an app whose `Contents/MacOS/UgreenDrive --render-previews DIR` draws every screen, light and dark, to PNGs.

### Releases

`mac/sign-release.sh vX.Y.Z` builds, Developer ID-signs (`ugd` first, then the app, hardened runtime), notarizes, staples, checks `spctl`, and uploads `Ugreen-Drive-mac.zip` plus its `SHA256SUMS` line to GitHub release `vX.Y.Z`, then marks it Latest. Signing material is read from a password manager by a loader script (`UGD_APPLE_CREDS_LOADER`), never stored in the repo. Forks must sign with their own Apple Developer ID and change the updater's repository in `mac/Sources/UgreenDrive/Updater.swift`.
