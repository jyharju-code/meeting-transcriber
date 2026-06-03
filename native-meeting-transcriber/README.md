# Native Meeting Transcriber Apps

Swift package for the native macOS pieces:

- `meeting-transcriber-dashboard`: visible dashboard, manual recording, floating HUD,
  format/model controls, and the dashboard-command recorder used by automatic mode.
- `native-meeting-recorder`: older helper recorder kept for direct CLI/manual testing.

The dashboard uses Apple's `ScreenCaptureKit` to record system audio and microphone
audio directly. It does not require BlackHole or audio-device routing.

## Build

```bash
swift build
```

## Install

```bash
./scripts/install_dashboard.sh
./scripts/install_native_recorder.sh
```

Installed apps:

```text
/Applications/Meeting Transcriber Dashboard.app
/Applications/Native Meeting Recorder.app
```

Open **Meeting Transcriber Dashboard.app** once and grant:

- Screen & System Audio Recording
- Microphone

Quit and reopen the app after permission changes.

## Why the Dashboard Records Automatic Meetings

macOS ties Screen/System Audio permissions to the exact process identity. The
Python watcher runs as a LaunchAgent, so the reliable design is:

```text
Python watcher detects Meet/Teams
-> writes ~/.meeting-transcriber/dashboard-command.json
-> dashboard app records with its own permission
-> Python worker transcribes the saved recording
```

This is why automatic recording depends on the dashboard app being installed and
available. The watcher tries to reopen it from `/Applications` if it is not open.

## Direct CLI Recorder Smoke Test

The helper recorder can still be tested directly:

```bash
"/Applications/Native Meeting Recorder.app/Contents/MacOS/native-meeting-recorder" \
  --output "$HOME/.meeting-transcriber/output/native-smoke-test.mp4" \
  --max-seconds 5
```

Stop early with `Ctrl-C`.

For normal use, prefer the dashboard app.
