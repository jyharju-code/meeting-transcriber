#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

swift build -c release --product meeting-transcriber-dashboard

APP_DIR="/Applications/Meeting Transcriber Dashboard.app"
mkdir -p "$APP_DIR/Contents/MacOS"
mkdir -p "$APP_DIR/Contents/Resources"

cp ".build/release/meeting-transcriber-dashboard" "$APP_DIR/Contents/MacOS/meeting-transcriber-dashboard"
chmod +x "$APP_DIR/Contents/MacOS/meeting-transcriber-dashboard"

ICONSET="$APP_DIR/Contents/Resources/AppIcon.iconset"
rm -rf "$ICONSET"
mkdir -p "$ICONSET"
python3 - "$ICONSET" <<'PY'
from pathlib import Path
import sys
from PIL import Image, ImageDraw, ImageFont

iconset = Path(sys.argv[1])
sizes = [
    (16, 1), (16, 2), (32, 1), (32, 2), (128, 1), (128, 2),
    (256, 1), (256, 2), (512, 1), (512, 2),
]

def draw_icon(size):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    radius = max(4, size // 8)
    draw.rounded_rectangle((0, 0, size - 1, size - 1), radius=radius, fill=(18, 24, 38, 255))
    pad = size // 6
    draw.rounded_rectangle((pad, size // 3, size - pad, size * 2 // 3), radius=max(2, size // 20), fill=(34, 197, 94, 255))
    bar_w = max(2, size // 18)
    xs = [size * 36 // 100, size * 48 // 100, size * 60 // 100]
    heights = [size // 5, size // 3, size // 4]
    for x, h in zip(xs, heights):
        draw.rounded_rectangle((x, size // 2 - h, x + bar_w, size // 2 + h), radius=max(1, bar_w // 2), fill=(255, 255, 255, 255))
    dot = size // 9
    draw.ellipse((size - pad - dot, pad, size - pad, pad + dot), fill=(239, 68, 68, 255))
    return img

for base, scale in sizes:
    pixels = base * scale
    name = f"icon_{base}x{base}{'@2x' if scale == 2 else ''}.png"
    draw_icon(pixels).save(iconset / name)
PY
iconutil -c icns "$ICONSET" -o "$APP_DIR/Contents/Resources/AppIcon.icns"

cat > "$APP_DIR/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleDevelopmentRegion</key>
  <string>en</string>
  <key>CFBundleExecutable</key>
  <string>meeting-transcriber-dashboard</string>
  <key>CFBundleIdentifier</key>
  <string>com.local.meeting-transcriber-dashboard</string>
  <key>CFBundleInfoDictionaryVersion</key>
  <string>6.0</string>
  <key>CFBundleName</key>
  <string>Meeting Transcriber Dashboard</string>
  <key>CFBundleIconFile</key>
  <string>AppIcon</string>
  <key>NSMicrophoneUsageDescription</key>
  <string>Meeting Transcriber Dashboard records your microphone during manual and automatic meeting recordings.</string>
  <key>CFBundlePackageType</key>
  <string>APPL</string>
  <key>CFBundleShortVersionString</key>
  <string>0.1.0</string>
  <key>CFBundleVersion</key>
  <string>1</string>
</dict>
</plist>
PLIST

SIGN_IDENTITY="${MEETING_TRANSCRIBER_CODESIGN_IDENTITY:-Local Meeting Transcriber Code Signing}"
if security find-identity -v -p codesigning | grep -F "$SIGN_IDENTITY" >/dev/null 2>&1; then
  codesign --force --deep --sign "$SIGN_IDENTITY" "$APP_DIR" >/dev/null
else
  codesign --force --sign - "$APP_DIR" >/dev/null 2>&1 || true
fi

PLIST="$HOME/Library/LaunchAgents/com.local.meeting-transcriber-dashboard.plist"
cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.local.meeting-transcriber-dashboard</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/open</string>
    <string>-a</string>
    <string>$APP_DIR</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>StandardOutPath</key>
  <string>$HOME/.meeting-transcriber/dashboard.out.log</string>
  <key>StandardErrorPath</key>
  <string>$HOME/.meeting-transcriber/dashboard.err.log</string>
</dict>
</plist>
PLIST

launchctl unload "$PLIST" >/dev/null 2>&1 || true
launchctl load "$PLIST" >/dev/null 2>&1 || true

echo "$APP_DIR"
