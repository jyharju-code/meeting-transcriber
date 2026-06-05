# Meeting Transcriber — Python Watcher & Worker

This folder holds the two Python pieces. For full setup, the macOS permission
trap, and the smoke test, see the [repository README](../README.md).

- `meeting_transcriber.py` — the LaunchAgent watcher. Polls browsers/Teams for an
  active Meet/Teams call and, after `start_after_consecutive_detections` hits,
  asks the dashboard app to record (or runs `record_command` directly).
- `transcribe_recording.py` — the worker. Splits the recording into snippets with
  `ffmpeg`, transcribes them via the selected provider, writes the transcript in
  the requested format, and (optionally) a Markdown summary with action items.
- `providers.py` — pluggable transcription/summary providers (see below).

## Engines (providers)

Transcription and summarization are provider-agnostic.

- **Transcription floor:** local **whisper.cpp** (`large-v3-turbo` by default) —
  no API key, no network once the model is cached. Install with
  [`./install_whisper.sh`](install_whisper.sh) (`brew install whisper-cpp` + model
  download). API transcription (OpenAI, or any OpenAI-compatible audio endpoint
  like Groq) unlocks by setting its key.
- **Summaries:** any **OpenAI-compatible** `/chat/completions` endpoint, chosen by
  config alone — OpenAI, **OpenRouter** (→ Claude, Gemini, Llama, Mistral, …),
  Groq, or a local Ollama server. No extra Python dependencies.

`transcribe_provider` / `summary_provider` pick the primary; `*_fallback` lists are
tried in order, then any other available provider, ending at the local floor. Keys
live in `~/.meeting-transcriber.env` (one `NAME=value` per line), never in
`config.json`.

> **v1 note:** provider selection is via `config.json`. The dashboard's model
> pickers still write the legacy top-level `transcribe_model`/`summary_model` keys,
> which apply only when no `providers` block is present. A dashboard provider
> picker is a follow-up.

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
| `transcribe_output_format` | `md` | `txt`, `md`, `json`, or `diarized_json` (diarized needs a diarization-capable provider; otherwise degrades to `json`). |
| `transcribe_provider` | `local_whisper`¹ | Primary transcription provider name. |
| `transcribe_fallback` | `["openai"]` | Providers tried if the primary is unavailable. |
| `summary_provider` | `openai` | Primary summary provider name. |
| `summary_fallback` | `["openrouter"]` | Providers tried if the primary is unavailable. |
| `providers` | see example | Registry: each entry may have a `transcribe` and/or `summarize` block. |
| `models_dir` | `~/.meeting-transcriber/models` | Where Whisper models are cached. |
| `whisper_auto_download` | `true` | Fetch the Whisper model on first use if missing. |
| `whisper_language` | `auto` | Whisper language hint (`auto`, `en`, `fi`, …). |
| `summary` | `on` | `on`/`off` to toggle the summary step. |
| `transcribe_model` / `diarize_model` / `summary_model` | — | **Legacy.** Used only when no `providers` block is present. |
| `meeting_owner` | `""` | When set, first-person action items are attributed to this name. Empty = neutral. |
| `meeting_owner_aliases` | `[]` | Extra names/spellings treated as the owner. |
| `summary_max_chars` | `120000` | Transcript chars sent to the summary prompt (truncation is logged). |
| `snippet_seconds` | `180` | Audio chunk length for transcription. |
| `min_transcribe_seconds` | `20` | Recordings shorter than this are skipped before any API call. |
| `max_parallel_transcriptions` | `3` | Concurrent snippet transcriptions. |
| `orphan_job_min_age_seconds` | `180` | Age before an artifact-less job folder is swept on startup. |
| `recorder_stop_grace_seconds` | `30` | Grace period when stopping the direct recorder. |

¹ Legacy configs with no `provider`/`transcribe_provider` keys default to `openai`
so existing setups keep their current behavior.
