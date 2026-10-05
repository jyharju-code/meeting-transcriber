# Meeting Transcriber: Python watcher and worker

This folder holds the two Python pieces. For full setup, the macOS permission
trap, and the smoke test, see the [repository README](../README.md).

- `meeting_transcriber.py` — the LaunchAgent watcher. Polls browsers/Teams for an
  active Meet/Teams call and, after `start_after_consecutive_detections` hits,
  asks the dashboard app to record (or runs `record_command` directly).
- `transcribe_recording.py` — the worker. Splits the recording into snippets with
  `ffmpeg`, transcribes them via the selected provider, writes the transcript in
  the requested format, and (optionally) a Markdown summary with action items.
- `providers.py` — transcription/summary providers: Soniox, Microsoft MAI (Azure Speech), Google.
- `policy.py` — the two switches, their lock into each meeting, the call log and the red lamp.
- `dual_mode.py` — Huipputaso, global position: Soniox + MAI, an adjudicator model listens (D9).
- `max_mode.py`, `tarkka.py` — Huipputaso with Gemini only (EU position and last fallback).

## Two switches decide where and how (docs/PAATOKSET.md)

The Dashboard shows two large switches, stored in `~/.meeting-transcriber/kytkimet.json`:

| Switch | Positions | Meaning |
|---|---|---|
| **Käsittelysijainti** | 🌍 **MAAILMANLAAJUINEN** (default) / 🇪🇺 **EU** | Global = best quality: Soniox, Microsoft MAI (Azure Speech, Sweden Central) and Google. EU = audio and text go to Gemini Enterprise Agent Platform EU only (`aiplatform.eu.rep.googleapis.com`, `gemini-3.5-flash`). |
| **Laatu** | ★ **HUIPPUTASO** (default, D10) / **PERUSTASO** | Perustaso = one pass. Huipputaso = a quick version right away, then several passes + an adjudicator model in the background, replacing it (the quick version is kept as `*-perustaso.*`). |

- The positions are locked into the meeting folder (`job.json`) when recording starts and again when
  transcription starts. **Stricter wins:** EU if it was on at either moment, Huipputaso likewise.
- **Nothing depends on the meeting title or content.**
- If the EU service does not answer, processing may continue outside the EU (Google only), and then
  the **red lamp** lights in the Dashboard and the transcript says so. The lamp also lights when the
  EU login (`gcloud auth application-default login`) stops working.
- Every outbound model call is logged in the meeting folder (`kutsut.jsonl`: host, model, result;
  never the API key), so afterwards you can show where a meeting was processed.
- OpenAI is not used at all.
- Global Huipputaso never stops on one service: Soniox + MAI → Soniox + Gemini → MAI + Gemini → Gemini only.
  Every downgrade is written into the transcript and notified. MAI uses the free Azure resource first,
  then the paid one up to `azure_paid_hours_per_month`.
- Failures notify (macOS), show a red row in the Dashboard and are retried with a growing delay for
  about a day. The raw recording is always kept.

| | perustaso (immediately) | Huipputaso (background) |
|---|---|---|
| 🌍 global | Soniox `stt-async-v5` (whole recording, speakers) | Soniox + MAI-Transcribe-2, adjudicator `gemini-3.8-flash` (EU) |
| 🇪🇺 EU | `gemini-3.5-flash` on Agent Platform EU | Flash EU ×2, adjudicator `gemini-3.8-flash` (EU) |

Summaries and other text steps run on Agent Platform EU in both positions (AI Studio only if EU fails).
Summaries follow `summary_language` (`auto` = same language as the transcript).

Keys live in `~/.meeting-transcriber.env` (`GEMINI_API_KEY`, `SONIOX_API_KEY`, `AZURE_SPEECH_F0_KEY`,
`AZURE_SPEECH_S0_KEY`), never in `config.json`.
The vocabulary (`sanasto.example.txt` → `~/.meeting-transcriber/sanasto.txt`) stays on your machine.

Testing from the command line: `--sijainti eu|maailmanlaajuinen`, `--laatu perus|huippu`
(recorded in `job.json`; EU still wins), `--huipputaso` (run a queued upgrade),
`--vain-yhteenveto` (redo the summary only).

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

## Merge a meeting that was split into several jobs

If a meeting still ends up in several job folders, stitch them into one
time-ordered Markdown document with gaps marked, plus one fresh summary:

```bash
~/.meeting-transcriber/venv/bin/python merge_fragments.py --auto \
  --since 20260930-1600 --until 20260930-1800 --config ~/.meeting-transcriber/app/config.json
```

The result is written to `<first job>/merged.md` (or `--out`). New jobs also
store `meeting.json` with the detected subject, which the merge uses as title.

## Tests

Unit tests: no network (every outbound call is mocked), no macOS APIs:

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
| `stop_after_consecutive_misses` | `4` | Misses in a row before a suppression is cleared (idle state). |
| `stop_after_consecutive_misses_while_recording` | `18` | Misses in a row before an **active** recording stops (18 × 10 s = 3 min), so a focus change never splits a meeting. |
| `teams_subject_memory_seconds` | `300` | How long a Teams meeting subject seen on the join screen / compact view is remembered, so the meeting window (whose title lacks the word "Meeting") still counts as the meeting. While recording, the subject is kept for the whole meeting. |
| `log_teams_window_titles` | `false` | Diagnostics: log every change in Teams window titles. |
| `catchup_transcription` | `true` | Background sweep that transcribes recordings left without a transcript (e.g. the capture stream stopped with an error, or the dashboard quit mid-transcription). |
| `catchup_min_age_minutes` / `catchup_max_age_hours` | `10` / `48` | Only recordings in this age window are swept; older backlog is left for you to decide. |
| `catchup_scan_minutes` | `5` | How often the sweep runs (one job at a time, never while recording). |
| `skip_silent_snippets` / `silence_mean_db` | `true` / `-45` | Do not send a snippet to a paid API when its mean level is below this (a 3-min snippet under -45 dBFS holds at most ~0.5 s of normal speech). Saves money on recordings that ran on after the meeting. |
| `max_recording_minutes` | `180` | Hard cap on a single recording. |
| `output_dir` | `~/.meeting-transcriber/output` | Where job folders are written. |
| `log_file` | `~/.meeting-transcriber/meeting-transcriber.log` | Watcher log. |
| `browser_apps` | Chrome, Edge, Brave, Arc, Safari | Browsers scanned for meeting tabs. |
| `recording_backend` | `dashboard_command` | `dashboard_command` (recommended) or empty to use `record_command`. |
| `dashboard_command_file` | `~/.meeting-transcriber/dashboard-command.json` | Command hand-off file the dashboard watches. |
| `dashboard_ack_file` | `~/.meeting-transcriber/dashboard-ack.json` | Dashboard acknowledgement read by the watcher. |
| `auto_suppression_file` | `~/.meeting-transcriber/auto-suppression.json` | Persistent latch created when the user stops an automatic recording. |
| `dashboard_app_path` | `/Applications/Meeting Transcriber Dashboard.app` | Dashboard app the watcher reopens if needed. |
| `teams_ignored_titles` | known prejoin titles | Teams page titles that must not trigger recording. |
| `record_command` | — | Argv for the direct backend; `{output}`/`{status}` are substituted. |
| `status_file` | `~/.meeting-transcriber/status.json` | Live recorder status (level meters, etc.). |
| `transcribe_after_recording` | `true` | Run the worker automatically when a recording finishes. |
| `transcribe_output_format` | `md` | `txt`, `md`, `json`, or `diarized_json` (perustaso writes plain `json`; Huipputaso has speakers). |
| `switch_file` / `lamp_file` | `~/.meeting-transcriber/kytkimet.json` / `lamppu.json` | Switch positions (Dashboard) and the red lamp state. |
| `global_perustaso` / `global_huipputaso` | `soniox` / `soniox_mai` | Global position routes (D8, D9); `gemini` restores the Google-only route. |
| `soniox_base_url` / `soniox_model` / `soniox_language_hints` | `https://api.soniox.com` / `stt-async-v5` / `["fi","en"]` | Soniox async API (EU project: `https://api.eu.soniox.com`). |
| `azure_mai_endpoints` | — | Azure Speech resources for MAI-Transcribe-2, tried in order: `{"name","url","key_env","paid"}`. |
| `azure_mai_locales` / `azure_paid_hours_per_month` | `["fi"]` / `50` | MAI language hint; monthly cap for the paid resource (usage in `azure_usage_file`). |
| `gemini_transcribe_model` / `gemini_transcribe_fallback_model` | `gemini-3.5-transcribe` / `gemini-3.5-flash` | Global position: AI Studio models (per-day limit on Transcribe switches to the fallback). |
| `gemini_transcribe_mode` | `smart` | Global perustaso mode (`smart` or `verbatim`); Huipputaso always uses verbatim. |
| `gemini_language_codes` | `["fi-FI"]` | Languages for gemini-3.5-transcribe. |
| `gemini_vocabulary_file` | — | One term per line (see `sanasto.example.txt`); keep your own outside the repo. |
| `agent_platform_project` / `agent_platform_location` | — / `eu` | Agent Platform project; only `eu` is allowed. Auth: gcloud ADC. |
| `agent_platform_audio_model` / `agent_platform_text_model` | `gemini-3.5-flash` | EU models for audio and text steps. |
| `max_adjudicator_model` | `gemini-3.8-flash` | Huipputaso adjudicator (EU). |
| `summary_provider` / `summary_fallback` | `agent_platform` / `["gemini"]` | Summary chain; only the named providers are used. |
| `snippet_split` / `snippet_seconds` / `snippet_max_seconds` | `silence` / `600` / `780` | Cut snippets at pauses near the target length (shared by perustaso and Huipputaso). |
| `models_dir` / `whisper_*` | — | Local whisper.cpp (explicit offline option only). |
| `summary` | `on` | `on`/`off` to toggle the summary step. |
| `meeting_owner` | `""` | When set, first-person action items are attributed to this name. Empty = neutral. |
| `meeting_owner_aliases` | `[]` | Extra names/spellings treated as the owner. |
| `summary_max_chars` | `120000` | Transcript chars sent to the summary prompt (truncation is logged). |
| `min_transcribe_seconds` | `20` | Recordings shorter than this are skipped before any API call. |
| `max_parallel_transcriptions` | `3` | Concurrent snippet transcriptions. |
| `orphan_job_min_age_seconds` | `180` | Age before an artifact-less job folder is swept on startup. |
| `recorder_stop_grace_seconds` | `30` | Grace period when stopping the direct recorder. |
