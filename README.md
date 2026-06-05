# Meeting Transcriber

[![CI](https://github.com/jyharju-code/meeting-transcriber/actions/workflows/ci.yml/badge.svg)](https://github.com/jyharju-code/meeting-transcriber/actions/workflows/ci.yml)

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
- **pluggable engines**: local Whisper floor + bring-your-own-key for any provider

## Engines

Transcription and summarization are provider-agnostic:

- **Transcription** defaults to a fully local **whisper.cpp** floor (no key, no
  network once cached). Install it with `meeting-transcriber/install_whisper.sh`.
  OpenAI (or any OpenAI-compatible audio API) unlocks by setting its key.
- **Summaries** run through any **OpenAI-compatible** chat endpoint — OpenAI,
  **OpenRouter** (→ Claude, Gemini, Llama, Mistral, …), Groq, or a local Ollama
  server — chosen by config alone, no extra dependencies.

You pick providers (and a fallback chain ending at the local floor) in
`config.json`; see [`meeting-transcriber/README.md`](meeting-transcriber/README.md).
With no API keys at all, recording + local transcription still work end-to-end.

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
- **for local transcription:** `whisper-cpp` + a model — run
  `meeting-transcriber/install_whisper.sh`
- **for API providers (optional):** a key for OpenAI and/or OpenRouter, etc.

## Setup

From the repo root:

```bash
cd meeting-transcriber
cp config.example.json config.json
```

Put any provider keys in a local env file, one per line. Do not commit this file.
Keys are optional — with none set, recording + local Whisper transcription still
work. Add only the providers you want:

```bash
umask 077
cat > ~/.meeting-transcriber.env <<'ENV'
OPENAI_API_KEY=your_openai_key_here
# OPENROUTER_API_KEY=your_openrouter_key_here
ENV
chmod 600 ~/.meeting-transcriber.env
```

(Optional) install the local Whisper floor so transcription works with no keys:

```bash
cd meeting-transcriber
./install_whisper.sh        # brew install whisper-cpp + downloads the model
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
`~/.meeting-transcriber/venv`, installs the pinned dependencies, and starts the
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

## Personalized Summaries (Optional)

By default the summary is speaker-neutral. To attribute first-person action items
to yourself, set these keys in `meeting-transcriber/config.json`:

```json
"meeting_owner": "Your Name",
"meeting_owner_aliases": ["Nickname", "Common misspelling"]
```

Leave `meeting_owner` empty for neutral notes.

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

## Development

```bash
# Python watcher/worker: byte-compile + unit tests (no network, no macOS APIs)
cd meeting-transcriber
python3 -m unittest discover -s tests -p "test_*.py" -v

# Native apps
cd ../native-meeting-transcriber
swift build
```

CI runs both on every push and pull request. See
[`meeting-transcriber/README.md`](meeting-transcriber/README.md) for the full
config-key reference.
