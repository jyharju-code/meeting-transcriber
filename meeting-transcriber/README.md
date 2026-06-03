# Meeting Transcriber — Python Watcher & Worker

This folder holds the two Python pieces. For full setup, the macOS permission
trap, and the smoke test, see the [repository README](../README.md).

- `meeting_transcriber.py` — the LaunchAgent watcher. Polls browsers/Teams for an
  active Meet/Teams call and, after `start_after_consecutive_detections` hits,
  asks the dashboard app to record (or runs `record_command` directly).
- `transcribe_recording.py` — the worker. Splits the recording into snippets with
  `ffmpeg`, transcribes them in parallel via the OpenAI API, writes the transcript
  in the requested format, and (optionally) a Markdown summary with action items.

## Install

```bash
./install_launch_agent.sh    # copies runtime to ~/.meeting-transcriber/app, makes a venv, loads the LaunchAgent
./uninstall_launch_agent.sh  # unloads and removes the LaunchAgent
```

Dependencies are pinned in [`requirements.txt`](requirements.txt).

## Run the worker by hand

```bash
~/.meeting-transcriber/venv/bin/python transcribe_recording.py \
  --recording ~/.meeting-transcriber/output/<job>/recording.mp4 \
  --config config.json
```

## Tests

Pure-function unit tests — no network, no macOS APIs, no OpenAI SDK required:

```bash
python3 -m unittest discover -s tests -p "test_*.py" -v
```

## Config reference (`config.json`)

Copy `config.example.json` to `config.json` and edit. Keys fall back to the
defaults shown when omitted. `~` is expanded in path values.

| Key | Default | Purpose |
|---|---|---|
| `poll_seconds` | `10` | Seconds between detection passes. |
| `start_after_consecutive_detections` | `2` | Hits in a row before recording starts (debounces false triggers). |
| `stop_after_consecutive_misses` | `4` | Misses in a row before recording stops. |
| `max_recording_minutes` | `180` | Hard cap on a single recording. |
| `output_dir` | `~/.meeting-transcriber/output` | Where job folders are written. |
| `log_file` | `~/.meeting-transcriber/meeting-transcriber.log` | Watcher log. |
| `browser_apps` | Chrome, Edge, Brave, Arc, Safari | Browsers scanned for meeting tabs. |
| `recording_backend` | `dashboard_command` | `dashboard_command` (recommended) or empty to use `record_command`. |
| `dashboard_command_file` | `~/.meeting-transcriber/dashboard-command.json` | Command hand-off file the dashboard watches. |
| `dashboard_app_path` | `/Applications/Meeting Transcriber Dashboard.app` | Dashboard app the watcher reopens if needed. |
| `record_command` | — | Argv for the direct backend; `{output}`/`{status}` are substituted. |
| `status_file` | `~/.meeting-transcriber/status.json` | Live recorder status (level meters, etc.). |
| `transcribe_after_recording` | `true` | Run the worker automatically when a recording finishes. |
| `transcribe_output_format` | `md` | `txt`, `md`, `json`, or `diarized_json`. |
| `transcribe_model` | `gpt-4o-mini-transcribe` | Transcription model. |
| `diarize_model` | `gpt-4o-transcribe-diarize` | Used when format is `diarized_json`. |
| `summary` | `on` | `on`/`off` to toggle the summary step. |
| `summary_model` | `gpt-4o-mini` | Chat model for the summary. |
| `meeting_owner` | `""` | When set, first-person action items are attributed to this name. Empty = neutral. |
| `meeting_owner_aliases` | `[]` | Extra names/spellings treated as the owner. |
| `summary_max_chars` | `120000` | Transcript chars sent to the summary prompt (truncation is logged). |
| `snippet_seconds` | `180` | Audio chunk length for transcription. |
| `min_transcribe_seconds` | `20` | Recordings shorter than this are skipped before any API call. |
| `max_parallel_transcriptions` | `3` | Concurrent snippet transcriptions. |
| `orphan_job_min_age_seconds` | `180` | Age before an artifact-less job folder is swept on startup. |
| `recorder_stop_grace_seconds` | `30` | Grace period when stopping the direct recorder. |
