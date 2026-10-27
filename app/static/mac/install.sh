#!/bin/sh
# Install Ugreen Drive for Mac: your Drive in Finder, from the menu bar.
#   curl -fsSL <drive>/mac/install.sh | sh
# Downloading with curl (not a browser) means macOS doesn't quarantine the app,
# so it opens without a Gatekeeper prompt. Run it again to update.
# Optional: UGD_VERSION=1.2.3 to pin a release.
set -eu

UGD_SERVER='__UGD_SERVER__'
REPO="neelsatyavolu/ugreen-drive"
ASSET="Ugreen-Drive-mac.zip"
APP_NAME="Ugreen Drive.app"
BUNDLE_ID="io.github.neelsatyavolu.ugreen-drive"

fail() { printf 'Ugreen Drive install: %s\n' "$1" >&2; exit 1; }

[ "$(uname -s)" = "Darwin" ] || fail "this installs the Mac app; it only runs on macOS."
MAJOR="$(sw_vers -productVersion | cut -d. -f1)"
[ "$MAJOR" -ge 13 ] 2>/dev/null || fail "macOS 13 (Ventura) or later is required."
for tool in curl shasum ditto; do
  command -v "$tool" >/dev/null 2>&1 || fail "$tool is required."
done

if [ -n "${UGD_DOWNLOAD_BASE:-}" ]; then
  BASE="$UGD_DOWNLOAD_BASE" # local testing only: checksums come from the same place
elif [ -n "${UGD_VERSION:-}" ]; then
  BASE="https://github.com/$REPO/releases/download/v$UGD_VERSION"
else
  BASE="https://github.com/$REPO/releases/latest/download"
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT INT TERM

printf 'Downloading Ugreen Drive for Mac…\n'
curl -fsSL "$BASE/$ASSET" -o "$TMP/$ASSET" || fail "download failed ($BASE/$ASSET)."
curl -fsSL "$BASE/SHA256SUMS" -o "$TMP/SHA256SUMS" || fail "checksum download failed."
(cd "$TMP" && grep " $ASSET\$" SHA256SUMS | shasum -a 256 -c - >/dev/null) \
  || fail "checksum mismatch — refusing to install."
ditto -x -k "$TMP/$ASSET" "$TMP/app" || fail "couldn't unpack the app."
[ -d "$TMP/app/$APP_NAME" ] || fail "the download doesn't contain $APP_NAME."

# /Applications needs an admin account; everyone else gets ~/Applications.
if [ -w /Applications ]; then DEST="/Applications"; else DEST="$HOME/Applications"; fi
mkdir -p "$DEST"

# Only this account's copy: other people on a shared Mac keep theirs running.
running() { pgrep -x -U "$(id -u)" UgreenDrive >/dev/null 2>&1; }
if running; then
  printf 'Closing the running app (it unmounts the drive first)…\n'
  osascript -e "quit app id \"$BUNDLE_ID\"" >/dev/null 2>&1 || true
  i=0
  while running && [ $i -lt 40 ]; do sleep 0.5; i=$((i + 1)); done
  if running; then
    fail "Ugreen Drive is still running. Quit it from the menu bar, then run this again."
  fi
fi

# Copy next to the old app first, so a failed copy never leaves you without one.
NEW="$DEST/.Ugreen Drive.app.installing"
rm -rf "$NEW"
ditto "$TMP/app/$APP_NAME" "$NEW" || { rm -rf "$NEW"; fail "couldn't copy the app into $DEST."; }
rm -rf "${DEST:?}/$APP_NAME"
mv "$NEW" "$DEST/$APP_NAME"

# Pre-fill this Drive's address unless one is already set.
if ! defaults read "$BUNDLE_ID" serverURL >/dev/null 2>&1; then
  defaults write "$BUNDLE_ID" serverURL "$UGD_SERVER"
fi

open "$DEST/$APP_NAME"
printf '\nInstalled %s in %s.\n' "$APP_NAME" "$DEST"
printf 'Click Sign in in the Ugreen Drive window and sign in with your NAS account.\n'
