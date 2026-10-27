#!/bin/sh
# Builds Ugreen Drive for Mac (menu bar app + Finder drive, with the bundled
# `ugd` CLI that runs the local WebDAV helper) and a release zip
# "build/Ugreen-Drive-mac.zip". Universal, ad-hoc signed (sign-release.sh signs
# with Developer ID and notarizes).
#   VERSION=0.1.0 DRIVE_URL=https://drive.example.com ./build.sh
#   CONFIGURATION=debug ./build.sh   # adds --render-previews (design review)
# DRIVE_URL is optional: without it people enter the address on first run (or
# the installer a Drive serves at /mac/install.sh fills it in). https, or http
# for a home-network address such as http://192.168.1.20:8790.
set -eu
cd "$(dirname "$0")"
VERSION="${VERSION:-0.0.0-dev}"
CONFIGURATION="${CONFIGURATION:-release}"
DRIVE_URL="${DRIVE_URL:-}"
case "$DRIVE_URL" in
  "" | https://* | http://*) ;;
  *) echo "DRIVE_URL must be an http(s):// origin" >&2; exit 2 ;;
esac
# Written into Info.plist with sed: keep to URL-safe characters. The app itself
# ignores an http address that isn't on the home network (AppConfig.origin).
if printf '%s' "$DRIVE_URL" | grep -q '[^A-Za-z0-9.:/_-]'; then
  echo "unexpected characters in $DRIVE_URL" >&2; exit 2
fi
APP="build/Ugreen Drive.app"
ZIP="build/Ugreen-Drive-mac.zip"
CLI_PKG="github.com/neelsatyavolu/ugreen-drive/cli/internal/app"
SWIFT_FLAGS="-c $CONFIGURATION --arch arm64 --arch x86_64"

rm -rf "$APP" "$ZIP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources" build/cli

(cd ../cli && for arch in arm64 amd64; do
  GOOS=darwin GOARCH=$arch CGO_ENABLED=0 go build -trimpath \
    -ldflags "-s -w -X $CLI_PKG.Version=$VERSION" -o "../mac/build/cli/ugd-$arch" .
done)
lipo -create -output "$APP/Contents/MacOS/ugd" build/cli/ugd-arm64 build/cli/ugd-amd64

# shellcheck disable=SC2086 # word splitting is intended
swift build $SWIFT_FLAGS
# shellcheck disable=SC2086
cp "$(swift build $SWIFT_FLAGS --show-bin-path)/UgreenDrive" "$APP/Contents/MacOS/"
sed -e "s/__VERSION__/$VERSION/g" -e "s|__DRIVE_URL__|$DRIVE_URL|g" Info.plist > "$APP/Contents/Info.plist"
cp -R Resources/Fonts "$APP/Contents/Resources/"
cp Resources/AppIcon.icns "$APP/Contents/Resources/"
# Start at login runs the app with --background through this agent (LoginItem.swift).
mkdir -p "$APP/Contents/Library/LaunchAgents"
cp Resources/io.github.neelsatyavolu.ugreen-drive.login.plist "$APP/Contents/Library/LaunchAgents/"

codesign --force --options runtime --sign - "$APP/Contents/MacOS/ugd"
codesign --force --options runtime --sign - "$APP"
codesign --verify --strict "$APP"
ditto -c -k --keepParent "$APP" "$ZIP"
echo "Built $APP and $ZIP ($VERSION, $CONFIGURATION)"
