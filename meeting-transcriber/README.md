# Meeting Transcriber

Small local-first macOS meeting recorder and transcriber.

It watches for Google Meet or Microsoft Teams calls, asks the native dashboard app
to record system audio + microphone audio, then chunks, transcribes, and summarizes
the recording with the OpenAI API.

The point is not to be a SaaS suite. It is a narrow tool:

- automatic Meet / Teams detection
- manual Start / Stop recording
- system audio + microphone recording through ScreenCaptureKit
- TXT / Markdown / JSON transcript output
- Markdown summary with action items first
- local output folders under `~/.meeting-transcriber/output`

## Consent

This records audio. Follow local law, event rules, and participant expectations.
For public broadcasts or conferences, use it like a personal note-taking recorder.

## Repo Layout

This project is usually published as a two-folder repo:

```text
meeting-transcriber/
native-meeting-transcriber/
```

`meeting-transcriber/` contains the Python watcher and transcription worker.
`native-meeting-transcriber/` contains the Swift dashboard app and native recorder.

## Requirements

- macOS 15+
- Xcode Command Line Tools
- Homebrew
- `ffmpeg`: `brew install ffmpeg`
- an OpenAI API key

## Setup

From the repo root:

```bash
cd meeting-transcriber
cp config.example.json config.json
```

Put your OpenAI key in a local env file. Do not commit this file.

```bash
printf 'OPENAI_API_KEY=%q\n' 'your_api_key_here' > ~/.meeting-transcriber.env
chmod 600 ~/.meeting-transcriber.env
```

Build and install the native apps:

```bash
cd ../native-meeting-transcriber
./scripts/install_native_recorder.sh
./scripts/install_dashboard.sh
```

Open **Meeting Transcriber Dashboard.app** once and grant:

- Screen & System Audio Recording
- Microphone

If macOS permissions were changed, quit and reopen the dashboard app. macOS often
does not apply ScreenCaptureKit permission changes until relaunch.

Install the watcher:

```bash
cd ../meeting-transcriber
./install_launch_agent.sh
```

The installer copies the Python runtime into `~/.meeting-transcriber/app`, creates
`~/.meeting-transcriber/venv`, installs the OpenAI Python package, and starts the
LaunchAgent.

## Manual Test

1. Open **Meeting Transcriber Dashboard.app**.
2. Play a speech clip, podcast, or YouTube video.
3. Click **Start**.
4. Speak for 30 seconds or let the clip play.
5. Click **Stop**.
6. Open the newest folder:

```bash
open ~/.meeting-transcriber/output
```

You should see:

```text
recording.mp4
snippets/
transcript.txt
transcript.md
summary.md
manifest.json
progress.json
```

## Automatic Test

Start a Google Meet or Teams meeting. After two watcher detections, the watcher
writes `~/.meeting-transcriber/dashboard-command.json`; the dashboard app performs
the actual recording because it owns the macOS Screen/System Audio permission.

Close the meeting tab/window to stop automatic recording.

## Why Dashboard Command Mode Exists

macOS permissions are tied to the process identity. A LaunchAgent-started helper
can be denied Screen/System Audio permission even when a manually opened app works.

The stable route is:

```text
watcher detects meeting -> dashboard command file -> dashboard records -> worker transcribes
```

That avoids audio-device fiddling and avoids asking users to install BlackHole.

## Costs

The app uses pay-as-you-go API calls. Short false triggers under
`min_transcribe_seconds` are skipped before any OpenAI API call.

In one local test run, roughly 7,500 tokens and 22 API calls cost about $0.02.
Your cost depends on meeting length, selected models, and summary settings.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Dashboard asks for permission again | Grant Screen/System Audio and Microphone, then quit and reopen the dashboard. |
| Automatic detection works but no recording starts | Keep the dashboard app running; the watcher will also try to reopen it from `/Applications`. |
| Empty output folders | Restart the watcher; startup cleanup removes old folders with no recording or transcript. |
| Very short recording has no transcript | Recordings under `min_transcribe_seconds` are intentionally skipped. |
| `OPENAI_API_KEY is not set` | Check `~/.meeting-transcriber.env`. |
| `ffmpeg` / `ffprobe` missing | Run `brew install ffmpeg`. |

## Uninstall

```bash
cd meeting-transcriber
./uninstall_launch_agent.sh
```

Then remove the installed apps if desired:

```bash
rm -rf "/Applications/Meeting Transcriber Dashboard.app"
rm -rf "/Applications/Native Meeting Recorder.app"
```
