#!/usr/bin/env bash
# Developer ID-sign, notarize and staple Ugreen Drive for Mac, then put
# Ugreen-Drive-mac.zip (and its line in SHA256SUMS) on GitHub release vX.Y.Z
# and make that release Latest. If CI already published the release (as a
# pre-release, with an ad-hoc-signed zip), its asset is replaced; otherwise the
# release is created. Run it on the maintainer's Mac:
#   mac/sign-release.sh v0.1.0
#   mac/sign-release.sh --local 0.1.0   # sign + notarize + staple only, no upload
#
# Credentials never touch this repo or GitHub: they're read from 1Password by
# the shared loader described in ~/Documents/GitHub/APPLE_SIGNING.md (Developer
# ID p12 into a temporary keychain, App Store Connect API key for notarytool).
# Override the loader with UGD_APPLE_CREDS_LOADER, the 1Password account with
# OP_ACCOUNT. Release builds have no DRIVE_URL: the installer each Drive serves
# (/mac/install.sh) fills in that Drive's address.
set -euo pipefail

LOCAL=0
if [ "${1:-}" = "--local" ]; then
  LOCAL=1
  shift
  TAG="v${1:?usage: mac/sign-release.sh --local X.Y.Z}"
else
  TAG="${1:?usage: mac/sign-release.sh vX.Y.Z | --local X.Y.Z}"
fi
printf '%s' "$TAG" | grep -Eq '^v[0-9]+\.[0-9]+\.[0-9]+$' || { echo "tag must look like v1.2.3" >&2; exit 2; }
VERSION="${TAG#v}"
cd "$(dirname "$0")"
APP="build/Ugreen Drive.app"
ASSET="Ugreen-Drive-mac.zip"
ZIP="build/$ASSET"
LOADER="${UGD_APPLE_CREDS_LOADER:-$HOME/Documents/GitHub/strix/scripts/load-apple-creds.sh}"
if [ "$LOCAL" = 0 ]; then
  REPO="$(gh repo view --json nameWithOwner -q .nameWithOwner)"
fi

echo "== 1/5 load Developer ID + notary credentials (1Password)"
# shellcheck disable=SC1090
source "$LOADER"
trap 'strix_cleanup_apple_creds >/dev/null 2>&1 || true' EXIT
IDENTITY="${APPLE_SIGNING_IDENTITY:?loader did not set APPLE_SIGNING_IDENTITY}"
KEYCHAIN_ARGS=()
[ -n "${STRIX_SIGN_KEYCHAIN:-}" ] && KEYCHAIN_ARGS=(--keychain "$STRIX_SIGN_KEYCHAIN")
KEY_PATH="${APPLE_API_KEY_PATH:?notary key missing}"
KEY_ID="${APPLE_API_KEY_ID:-${APPLE_API_KEY:-}}"
ISSUER="${APPLE_API_ISSUER:?notary issuer missing}"

echo "== 2/5 build $VERSION and sign with $IDENTITY"
VERSION="$VERSION" DRIVE_URL="" ./build.sh
sign() {
  codesign --force --timestamp --options runtime --sign "$IDENTITY" ${KEYCHAIN_ARGS[@]+"${KEYCHAIN_ARGS[@]}"} "$@"
}
sign "$APP/Contents/MacOS/ugd" # nested code first, then the bundle
sign "$APP"
codesign --verify --strict --deep "$APP"
codesign -dv --verbose=2 "$APP" 2>&1 | grep -E "^(Authority|TeamIdentifier)="

echo "== 3/5 notarize (Apple usually takes a few minutes)"
rm -f "$ZIP"
ditto -c -k --keepParent "$APP" "$ZIP"
RESULT="$(xcrun notarytool submit "$ZIP" --key "$KEY_PATH" --key-id "$KEY_ID" --issuer "$ISSUER" \
  --wait --timeout 45m --output-format json)"
STATUS="$(printf '%s' "$RESULT" | /usr/bin/python3 -c 'import json,sys; print(json.load(sys.stdin).get("status",""))')"
if [ "$STATUS" != "Accepted" ]; then
  ID="$(printf '%s' "$RESULT" | /usr/bin/python3 -c 'import json,sys; print(json.load(sys.stdin).get("id",""))')"
  echo "notarization $STATUS" >&2
  [ -n "$ID" ] && xcrun notarytool log "$ID" --key "$KEY_PATH" --key-id "$KEY_ID" --issuer "$ISSUER" >&2
  exit 1
fi

echo "== 4/5 staple and check Gatekeeper"
xcrun stapler staple "$APP"
xcrun stapler validate "$APP"
spctl --assess --type execute --verbose=2 "$APP" 2>&1 | tee /dev/stderr | grep -q "Notarized Developer ID"
rm -f "$ZIP"
ditto -c -k --keepParent "$APP" "$ZIP" # re-zip with the stapled ticket

if [ "$LOCAL" = 1 ]; then
  echo "Signed and notarized $APP and $ZIP (not uploaded)"
  exit 0
fi

echo "== 5/5 upload to release $TAG"
WORK="$(mktemp -d)"
if gh release view "$TAG" --repo "$REPO" >/dev/null 2>&1; then
  gh release download "$TAG" --repo "$REPO" --pattern SHA256SUMS --dir "$WORK" 2>/dev/null || touch "$WORK/SHA256SUMS"
else
  gh release create "$TAG" --repo "$REPO" --prerelease --title "Ugreen Drive $VERSION" \
    --notes "Ugreen Drive for Mac $VERSION. Install it from your Drive: Mac app in the sidebar."
  touch "$WORK/SHA256SUMS"
fi
grep -v " $ASSET\$" "$WORK/SHA256SUMS" > "$WORK/SHA256SUMS.new" || true
(cd build && shasum -a 256 "$ASSET") >> "$WORK/SHA256SUMS.new"
mv "$WORK/SHA256SUMS.new" "$WORK/SHA256SUMS"
gh release upload "$TAG" --repo "$REPO" --clobber "$ZIP" "$WORK/SHA256SUMS"
rm -rf "$WORK"
# Installed apps and the install script follow releases/latest.
gh release edit "$TAG" --repo "$REPO" --prerelease=false --latest >/dev/null
echo "Signed, notarized and published $ASSET on $TAG (now Latest)"
