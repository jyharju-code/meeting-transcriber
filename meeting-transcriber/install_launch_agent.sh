#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
SOURCE_ROOT="$(pwd)"
ROOT="$HOME/.meeting-transcriber/app"
PLIST="$HOME/Library/LaunchAgents/com.local.meeting-transcriber.plist"
CONFIG="$ROOT/config.json"
VENV="$HOME/.meeting-transcriber/venv"

mkdir -p "$ROOT" "$HOME/.meeting-transcriber/output"

for file in meeting_transcriber.py transcribe_recording.py run_watcher.sh uninstall_launch_agent.sh config.example.json; do
  cp "$SOURCE_ROOT/$file" "$ROOT/$file"
done
chmod +x "$ROOT/"*.sh "$ROOT/"*.py

if [[ ! -f "$CONFIG" ]]; then
  if [[ -f "$SOURCE_ROOT/config.json" ]]; then
    cp "$SOURCE_ROOT/config.json" "$CONFIG"
  else
    cp "$ROOT/config.example.json" "$CONFIG"
  fi
  echo "Created $CONFIG. Install and permit the dashboard app before expecting recordings."
fi

if [[ ! -x "$VENV/bin/python" ]]; then
  /usr/bin/python3 -m venv "$VENV"
fi
"$VENV/bin/python" -m pip install --upgrade pip openai >/dev/null

cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.local.meeting-transcriber</string>
  <key>ProgramArguments</key>
  <array>
    <string>$ROOT/run_watcher.sh</string>
  </array>
  <key>WorkingDirectory</key>
  <string>$ROOT</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>$ROOT/launchd.out.log</string>
  <key>StandardErrorPath</key>
  <string>$ROOT/launchd.err.log</string>
</dict>
</plist>
PLIST

launchctl unload "$PLIST" >/dev/null 2>&1 || true
launchctl load "$PLIST"
echo "Installed and started com.local.meeting-transcriber"
echo "Logs: $ROOT/meeting-transcriber.log and $ROOT/launchd.err.log"
