#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

swift build -c release

INSTALL_DIR="$HOME/.meeting-transcriber/bin"
APP_DIR="/Applications/Native Meeting Recorder.app"
mkdir -p "$INSTALL_DIR"
mkdir -p "$APP_DIR/Contents/MacOS"
mkdir -p "$APP_DIR/Contents/Resources"

cp ".build/release/native-meeting-recorder" "$APP_DIR/Contents/MacOS/native-meeting-recorder"
chmod +x "$APP_DIR/Contents/MacOS/native-meeting-recorder"

cat > "$APP_DIR/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleDevelopmentRegion</key>
  <string>en</string>
  <key>CFBundleExecutable</key>
  <string>native-meeting-recorder</string>
  <key>CFBundleIdentifier</key>
  <string>com.local.native-meeting-recorder</string>
  <key>CFBundleInfoDictionaryVersion</key>
  <string>6.0</string>
  <key>CFBundleName</key>
  <string>Native Meeting Recorder</string>
  <key>CFBundlePackageType</key>
  <string>APPL</string>
  <key>CFBundleShortVersionString</key>
  <string>0.1.0</string>
  <key>CFBundleVersion</key>
  <string>1</string>
  <key>LSBackgroundOnly</key>
  <true/>
  <key>NSMicrophoneUsageDescription</key>
  <string>Native Meeting Recorder records your microphone during meetings so transcripts include your voice.</string>
</dict>
</plist>
PLIST

SIGN_IDENTITY="${MEETING_TRANSCRIBER_CODESIGN_IDENTITY:-Local Meeting Transcriber Code Signing}"
if security find-identity -v -p codesigning | grep -F "$SIGN_IDENTITY" >/dev/null 2>&1; then
  codesign --force --deep --sign "$SIGN_IDENTITY" "$APP_DIR" >/dev/null
else
  codesign --force --sign - "$APP_DIR" >/dev/null 2>&1 || true
fi
ln -sf "$APP_DIR/Contents/MacOS/native-meeting-recorder" "$INSTALL_DIR/native-meeting-recorder"

echo "$APP_DIR/Contents/MacOS/native-meeting-recorder"
