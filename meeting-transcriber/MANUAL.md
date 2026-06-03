# Manual Smoke Test

Use this when you want to prove the app works without joining a real meeting.

## 1. Prepare

```bash
brew install ffmpeg
cd meeting-transcriber
cp config.example.json config.json
printf 'OPENAI_API_KEY=%q\n' 'your_api_key_here' > ~/.meeting-transcriber.env
chmod 600 ~/.meeting-transcriber.env
```

Build and install the native apps:

```bash
cd ../native-meeting-transcriber
./scripts/install_native_recorder.sh
./scripts/install_dashboard.sh
```

Install the Python watcher/runtime:

```bash
cd ../meeting-transcriber
./install_launch_agent.sh
```

Open **Meeting Transcriber Dashboard.app** and grant:

- Screen & System Audio Recording
- Microphone

Quit and reopen the dashboard after granting permissions.

## 2. Record 30 Seconds

1. Play clear speech on your Mac, for example a public YouTube talk or podcast.
2. Click **Start** in the dashboard.
3. Let it run for 30-60 seconds.
4. Click **Stop**.

The dashboard should show progress through splitting, transcribing, summarizing,
and done.

## 3. Inspect Output

```bash
open ~/.meeting-transcriber/output
```

Open the newest folder. Expected files:

```text
recording.mp4
snippets/snippet-000.m4a
transcript.txt
transcript.md
summary.md
manifest.json
progress.json
```

Confirm:

- `transcript.md` contains each spoken paragraph once.
- `summary.md` starts with action items.
- `progress.json` ends with `"stage": "done"`.

## 4. Short Recording Skip

Record for less than 10 seconds and stop. Expected:

- no snippets
- no transcript API call
- `progress.json` contains `"stage": "skipped"`

## 5. Automatic Mode

Start a Google Meet or Teams call. The watcher detects the meeting and writes a
dashboard command; the dashboard app records using its already-granted macOS
permissions.

Close the meeting tab/window to stop automatic recording.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Screen/System Audio Recording permission was not granted` | Enable the dashboard in System Settings, then quit and reopen it. |
| Audio meters do not move | Check that speech is playing and the Mac output is not muted. |
| `OPENAI_API_KEY is not set` | Recreate `~/.meeting-transcriber.env`. |
| `ffmpeg` or `ffprobe` missing | Run `brew install ffmpeg`. |
| Empty automatic folders | Restart the watcher; old empty folders are swept on startup. |
