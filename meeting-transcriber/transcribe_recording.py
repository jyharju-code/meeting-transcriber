#!/usr/bin/env python3
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import policy
import providers


DEFAULT_CHUNK_SECONDS = 180
DEFAULT_MIN_TRANSCRIBE_SECONDS = 20
DEFAULT_TRANSCRIBE_MODEL = "gpt-4o-mini-transcribe"
DEFAULT_DIARIZE_MODEL = "gpt-4o-transcribe-diarize"
DEFAULT_SUMMARY_MODEL = "gpt-4o-mini"
DEFAULT_SUMMARY_MAX_CHARS = 120_000


def load_config(path: Path | None) -> dict[str, Any]:
    if not path or not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def write_progress(path: Path | None, **payload: Any) -> None:
    if not path:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    payload["updatedAt"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def output_paths(job_dir: Path) -> dict[str, Path]:
    return {
        "txt": job_dir / "transcript.txt",
        "md": job_dir / "transcript.md",
        "json": job_dir / "transcript.json",
        "diarized_json": job_dir / "transcript.diarized.json",
        "summary": job_dir / "summary.md",
        "manifest": job_dir / "manifest.json",
        "progress": job_dir / "progress.json",
    }


def ensure_job(recording: Path, output_root: Path) -> tuple[Path, Path]:
    recording = recording.expanduser().resolve()
    output_root = output_root.expanduser().resolve()
    if recording.parent == output_root:
        job_dir = output_root / recording.stem
        job_dir.mkdir(parents=True, exist_ok=True)
        target = job_dir / f"recording{recording.suffix}"
        if recording.exists() and recording != target:
            shutil.move(str(recording), str(target))
        return job_dir, target
    recording.parent.mkdir(parents=True, exist_ok=True)
    return recording.parent, recording


def ffmpeg_bin() -> str:
    return "/opt/homebrew/bin/ffmpeg" if Path("/opt/homebrew/bin/ffmpeg").exists() else "ffmpeg"


def detect_silences(recording: Path, noise_db: float = -35.0, min_silence: float = 0.4) -> list[tuple[float, float]]:
    """[(start, end)] of pauses in the first audio track, via ffmpeg silencedetect."""
    import re

    result = subprocess.run(
        [ffmpeg_bin(), "-hide_banner", "-nostats", "-i", str(recording), "-map", "0:a:0", "-vn",
         "-af", f"silencedetect=noise={noise_db}dB:d={min_silence}", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    silences: list[tuple[float, float]] = []
    start = None
    for line in result.stderr.splitlines():
        m = re.search(r"silence_start:\s*([0-9.]+)", line)
        if m:
            start = float(m.group(1))
            continue
        m = re.search(r"silence_end:\s*([0-9.]+)", line)
        if m and start is not None:
            silences.append((start, float(m.group(1))))
            start = None
    return silences


def choose_cuts(duration: float, silences: list[tuple[float, float]], target: float,
                max_len: float, window: float) -> list[tuple[float, float]]:
    """Split [0, duration] into pieces of about `target` seconds (never above `max_len`),
    cutting in the middle of the longest pause found within `target ± window`.
    Falls back to a hard cut at `target` when there is no pause in the window."""
    pieces: list[tuple[float, float]] = []
    pos = 0.0
    while duration - pos > max_len:
        lo, hi = pos + target - window, min(pos + target + window, pos + max_len)
        best = None
        for s, e in silences:
            mid = (s + e) / 2.0
            if lo <= mid <= hi:
                length = e - s
                score = (length, -abs(mid - (pos + target)))
                if best is None or score > best[0]:
                    best = (score, mid)
        cut = best[1] if best else pos + target
        pieces.append((pos, cut))
        pos = cut
    pieces.append((pos, duration))
    return pieces


def split_snippets(recording: Path, snippets_dir: Path, chunk_seconds: int, progress: Path | None,
                   max_seconds: int | None = None, split_mode: str = "fixed",
                   window_seconds: int = 120, silence_db: float = -35.0) -> list[Path]:
    snippets_dir.mkdir(parents=True, exist_ok=True)
    existing = sorted(snippets_dir.glob("snippet-*.m4a"))
    if existing:
        return existing

    if split_mode == "silence":
        duration = recording_duration_seconds(recording)
        if duration:
            write_progress(progress, stage="splitting", progress=0.04, message="Finding pauses for snippet cuts")
            max_len = float(max_seconds or chunk_seconds * 1.3)
            pieces = choose_cuts(duration, detect_silences(recording, silence_db), float(chunk_seconds),
                                 max_len, float(window_seconds))
            write_progress(progress, stage="splitting", progress=0.05,
                           message=f"Splitting recording into {len(pieces)} snippets at pauses")
            for i, (start, end) in enumerate(pieces):
                out = snippets_dir / f"snippet-{i:03d}.m4a"
                result = subprocess.run(
                    [ffmpeg_bin(), "-hide_banner", "-y", "-ss", f"{start:.3f}", "-to", f"{end:.3f}",
                     "-i", str(recording), "-map", "0:a:0", "-vn", "-c:a", "aac", str(out)],
                    capture_output=True, text=True,
                )
                if result.returncode != 0:
                    raise RuntimeError(result.stderr.strip() or "ffmpeg snippet cut failed")
            (snippets_dir / "cuts.json").write_text(
                json.dumps([{"file": f"snippet-{i:03d}.m4a", "start": s, "end": e}
                            for i, (s, e) in enumerate(pieces)], indent=2),
                encoding="utf-8",
            )
            return sorted(snippets_dir.glob("snippet-*.m4a"))

    write_progress(progress, stage="splitting", progress=0.05, message="Splitting recording into snippets")
    cmd = [
        "/opt/homebrew/bin/ffmpeg" if Path("/opt/homebrew/bin/ffmpeg").exists() else "ffmpeg",
        "-hide_banner",
        "-y",
        "-i",
        str(recording),
        "-map",
        "0:a:0",
        "-vn",
        "-c:a",
        "aac",
        "-f",
        "segment",
        "-segment_time",
        str(chunk_seconds),
        "-reset_timestamps",
        "1",
        str(snippets_dir / "snippet-%03d.m4a"),
    ]
    result = subprocess.run(cmd, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "ffmpeg snippet split failed")
    snippets = sorted(snippets_dir.glob("snippet-*.m4a"))
    if not snippets:
        raise RuntimeError("No snippets were created")
    return snippets


def ffprobe_path() -> str:
    return "/opt/homebrew/bin/ffprobe" if Path("/opt/homebrew/bin/ffprobe").exists() else "ffprobe"


def recording_duration_seconds(recording: Path) -> float | None:
    cmd = [
        ffprobe_path(),
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(recording),
    ]
    result = subprocess.run(cmd, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        return None
    try:
        return float(result.stdout.strip())
    except ValueError:
        return None


def transcript_text(result: Any) -> str:
    text = getattr(result, "text", None)
    if isinstance(text, str):
        return text
    if isinstance(result, dict) and isinstance(result.get("text"), str):
        return result["text"]
    return str(result)


def result_jsonable(result: Any) -> Any:
    if hasattr(result, "model_dump"):
        return result.model_dump()
    if isinstance(result, (dict, list)):
        return result
    return {"text": transcript_text(result)}


DEFAULT_SILENCE_MEAN_DB = -45.0


def snippet_mean_volume(snippet: Path) -> float | None:
    """Mean volume (dBFS) of a snippet via ffmpeg volumedetect, or None if unknown."""
    ffmpeg = "/opt/homebrew/bin/ffmpeg" if Path("/opt/homebrew/bin/ffmpeg").exists() else "ffmpeg"
    result = subprocess.run(
        [ffmpeg, "-hide_banner", "-nostats", "-i", str(snippet), "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    for line in result.stderr.splitlines():
        if "mean_volume:" in line:
            try:
                return float(line.split("mean_volume:")[1].split("dB")[0])
            except (IndexError, ValueError):
                return None
    return None


def snippet_is_silent(mean_db: float | None, threshold_db: float | None) -> bool:
    """A 3-minute snippet whose mean level is below about -45 dBFS holds at most
    ~0.5 s of normal-level speech (one second of speech already lifts the mean to
    about -42 dB), so it is not worth sending to a paid API."""
    return threshold_db is not None and mean_db is not None and mean_db < threshold_db


def transcribe_snippets(
    transcriber: "providers.Transcriber",
    snippets: list[Path],
    job_dir: Path,
    max_parallel: int,
    progress: Path | None,
    silence_threshold_db: float | None = None,
) -> tuple[str, list[dict[str, Any]], list[Any]]:
    snippet_transcript_dir = job_dir / "snippets" / "transcripts"
    snippet_transcript_dir.mkdir(parents=True, exist_ok=True)
    text_parts: list[str] = []
    json_chunks: list[dict[str, Any]] = []
    diarized_chunks: list[Any] = []

    use_diarized = bool(getattr(transcriber, "diarize", False))
    effective_parallel = max(1, min(max_parallel, getattr(transcriber, "max_parallel", max_parallel)))

    def transcribe_one(index: int, snippet: Path) -> dict[str, Any]:
        # Resume: a snippet already transcribed by this same engine in an earlier,
        # interrupted run is reused instead of being paid for again.
        saved = snippet_transcript_dir / f"{snippet.stem}.json"
        if saved.exists():
            try:
                previous = json.loads(saved.read_text(encoding="utf-8"))
                prev_mode = (previous.get("raw") or {}).get("mode") if isinstance(previous.get("raw"), dict) else None
                same_mode = prev_mode is None or prev_mode == str(getattr(transcriber, "mode", prev_mode))
                if previous.get("model") == transcriber.model_label and same_mode and str(previous.get("text", "")).strip():
                    return previous
            except (OSError, ValueError):
                pass
        if silence_threshold_db is not None:
            mean_db = snippet_mean_volume(snippet)
            if snippet_is_silent(mean_db, silence_threshold_db):
                payload = {"index": index, "file": str(snippet), "model": transcriber.model_label,
                           "text": "", "raw": {"skipped": "silence", "mean_db": mean_db}}
                saved.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
                return payload
        text, raw = transcriber.transcribe(snippet)
        payload: dict[str, Any] = {
            "index": index,
            "file": str(snippet),
            "model": transcriber.model_label,
            "text": text,
            "raw": raw,
        }
        (snippet_transcript_dir / f"{snippet.stem}.json").write_text(
            json.dumps(payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return payload

    payloads: dict[int, dict[str, Any]] = {}
    completed = 0
    with ThreadPoolExecutor(max_workers=effective_parallel) as pool:
        futures = {
            pool.submit(transcribe_one, index, snippet): (index, snippet)
            for index, snippet in enumerate(snippets, start=1)
        }
        for future in as_completed(futures):
            index, _snippet = futures[future]
            payloads[index] = future.result()
            completed += 1
            pct = 0.1 + 0.65 * (completed / max(len(snippets), 1))
            write_progress(
                progress,
                stage="transcribing",
                progress=round(pct, 3),
                message=f"Transcribed {completed} of {len(snippets)} snippets",
                current=completed,
                total=len(snippets),
            )

    for index in sorted(payloads):
        # Payloads are keyed by original snippet index, so completion order cannot affect transcript order.
        payload = payloads[index]
        json_chunks.append(payload)
        text_parts.append(str(payload.get("text", "")).strip())
        if use_diarized:
            diarized_chunks.append(payload)

    full_text = "\n\n".join(part for part in text_parts if part)
    write_progress(progress, stage="transcribing", progress=0.78, message="Combining transcript")
    return full_text, json_chunks, diarized_chunks


def write_transcript_outputs(job_dir: Path, requested_format: str, text: str, chunks: list[dict[str, Any]], diarized_chunks: list[Any]) -> None:
    paths = output_paths(job_dir)
    paths["txt"].write_text(text + "\n", encoding="utf-8")

    if requested_format == "md":
        paths["md"].write_text("# Transcript\n\n" + text + "\n", encoding="utf-8")
    elif requested_format == "json":
        paths["json"].write_text(
            json.dumps({"text": text, "chunks": chunks}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    elif requested_format == "diarized_json":
        paths["diarized_json"].write_text(
            json.dumps({"text": text, "chunks": diarized_chunks or chunks}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )


def build_summary_prompt(
    text: str,
    owner: str = "",
    aliases: list[str] | None = None,
    max_chars: int = DEFAULT_SUMMARY_MAX_CHARS,
    language: str = "auto",
    meeting_date: str = "",
) -> str:
    """Build the meeting-notes prompt.

    When `owner` is set, action items spoken in the first person or by the
    listed `aliases` are attributed to that person; otherwise the prompt stays
    speaker-neutral. This keeps the tool free of any hard-coded identity.
    """
    owner = (owner or "").strip()
    alias_list = [a.strip() for a in (aliases or []) if a and a.strip()]

    owner_rules = ""
    if owner:
        alias_clause = ""
        if alias_list:
            alias_clause = (
                f"\n- Treat these as references to {owner}: "
                + ", ".join([owner, *alias_list, '"me"'])
                + "."
            )
        owner_rules = (
            f"\n- The meeting owner is {owner}.{alias_clause}"
            f'\n- Include a section for {owner} if they have action items.'
        )

    intro = f"You are preparing meeting notes for {owner}." if owner else "You are preparing meeting notes."

    if not language or language.lower() == "auto":
        language_rule = (
            "Write the notes in the SAME LANGUAGE as the transcript, detected "
            "automatically (a Finnish transcript gets Finnish notes, an English one "
            "English notes), and localize the section headings to that language."
        )
    else:
        language_rule = f"Write the notes and section headings in {language}."

    date_rule = (
        f"\n- The meeting took place on {meeting_date}. Dates spoken without a year refer to the next "
        "occurrence on or after that day (or the stated context); never write an earlier year. If unsure, omit the year."
        if meeting_date else ""
    )

    return f"""
{intro}

{language_rule}

Create concise Markdown meeting notes from this transcript.

Rules:
- Put ACTION ITEMS first.
- Action items must be grouped by person when a responsible person can be inferred.{owner_rules}
- If ownership is unclear, put it under an "Unassigned" heading (localized to the notes' language).
- After action items, include Decisions, Key Points, Risks/Open Questions, and Short Summary.
- Do not invent facts not supported by the transcript.{date_rule}

Transcript:
{text[:max_chars]}
""".strip()


def summarize(
    summarizer: "providers.Summarizer | None",
    job_dir: Path,
    text: str,
    progress: Path | None,
    owner: str = "",
    aliases: list[str] | None = None,
    max_chars: int = DEFAULT_SUMMARY_MAX_CHARS,
    language: str = "auto",
) -> None:
    paths = output_paths(job_dir)
    write_progress(progress, stage="summarizing", progress=0.84, message="Creating summary and action items")
    if not text.strip():
        paths["summary"].write_text("# Summary\n\nNo transcript text was available.\n", encoding="utf-8")
        return

    if summarizer is None:
        paths["summary"].write_text(
            "# Summary\n\nNo summary provider was available (no API key set and no "
            "local LLM configured). The full transcript is in this folder.\n",
            encoding="utf-8",
        )
        print("transcribe_recording: no summary provider available; wrote placeholder", file=sys.stderr)
        return

    if len(text) > max_chars:
        print(
            f"transcribe_recording: transcript is {len(text)} chars; "
            f"truncating to {max_chars} for the summary prompt.",
            file=sys.stderr,
        )

    stamp = re.match(r"(\d{4})(\d{2})(\d{2})-", job_dir.name)
    meeting_date = f"{stamp.group(1)}-{stamp.group(2)}-{stamp.group(3)}" if stamp else time.strftime("%Y-%m-%d")
    prompt = build_summary_prompt(text, owner=owner, aliases=aliases, max_chars=max_chars, language=language,
                                  meeting_date=meeting_date)
    summary = summarizer.summarize(prompt)
    if not summary.lstrip().startswith("#"):
        summary = "# Meeting Summary\n\n" + summary
    paths["summary"].write_text(summary.strip() + "\n", encoding="utf-8")


# --------------------------------------------------------------------------- #
# Kytkimet -> käsittely (docs/PAATOKSET.md D1-D5)
#
#   sijainti  laatu    perustaso (heti)                         Huipputaso (taustalla, --huipputaso)
#   global    perus    gemini-3.5-transcribe smart (AI Studio)  -
#   global    huippu   gemini-3.5-transcribe verbatim+sanasto   Transcribe ×2 + Flash EU ×2 + tuomari EU
#   eu        perus    gemini-3.5-flash (Agent Platform EU)     -
#   eu        huippu   gemini-3.5-flash (Agent Platform EU)     Flash EU ×2 + tuomari EU
#
# The perustaso pass in Huipputaso mode is exactly Huipputaso's first pass, so it is reused (D5).
# In EU mode, AI Studio is used only if the EU service fails (D3); the call log then lights the
# red lamp (D4). Nothing here depends on the meeting title.
# --------------------------------------------------------------------------- #

UPGRADE_QUEUE = "huipputaso-jono.json"
UPGRADE_PROGRESS = "progress-huipputaso.json"


def build_transcriber(config: dict[str, Any], settings: dict[str, str]) -> "providers.Transcriber | None":
    vocab = providers.load_vocabulary(config)
    ffmpeg = providers.ffmpeg_path(config)
    langs = list(config.get("gemini_language_codes") or ["fi-FI"])
    huippu = settings["laatu"] == policy.HUIPPU
    studio = providers.GeminiTranscriber(
        "gemini",
        model=config.get("gemini_transcribe_model", providers.GEMINI_TRANSCRIBE_MODEL),
        fallback_model=config.get("gemini_transcribe_fallback_model", providers.GEMINI_FALLBACK_MODEL),
        mode="verbatim" if huippu else config.get("gemini_transcribe_mode", "smart"),
        language_codes=langs,
        ffmpeg=ffmpeg,
        custom_vocabulary=vocab,
    )
    if settings["sijainti"] == policy.EU:
        project, location, _ = providers.ap_settings(config)
        eu = providers.AgentPlatformTranscriber(
            "agent_platform_eu", project, location,
            str(config.get("agent_platform_audio_model", providers.GEMINI_FALLBACK_MODEL)),
            ffmpeg=ffmpeg, custom_vocabulary=vocab, fallback=studio if studio.available() else None,
        )
        if eu.available():
            return eu
        print("transcribe_recording: EU service unavailable; using AI Studio (red lamp)", file=sys.stderr)
    return studio if studio.available() else None


def build_summarizer(config: dict[str, Any]) -> "providers.Summarizer | None":
    return providers.select(
        providers.build_summarizers(config),
        config.get("summary_provider", "agent_platform"),
        config.get("summary_fallback", ["gemini"]),
    )


def summary_options(config: dict[str, Any], args: argparse.Namespace | None = None) -> dict[str, Any]:
    aliases = config.get("meeting_owner_aliases") or []
    return {
        "owner": str(config.get("meeting_owner", "") or ""),
        "aliases": aliases if isinstance(aliases, list) else [],
        "max_chars": int(config.get("summary_max_chars", DEFAULT_SUMMARY_MAX_CHARS)),
        "language": (getattr(args, "summary_language", None) if args else None) or config.get("summary_language") or "auto",
    }


def summarize_safely(config: dict[str, Any], job_dir: Path, text: str, progress: Path | None,
                     options: dict[str, Any]) -> tuple[bool, str | None]:
    """Write summary.md. A failure is recorded (stage "summary_error") so the watcher retries the
    summary alone; the transcript is kept either way."""
    summarizer = build_summarizer(config)
    label = summarizer.model_label if summarizer else None
    try:
        summarize(summarizer, job_dir, text, progress, **options)
        return True, label
    except Exception as exc:  # noqa: BLE001
        write_progress(progress, stage="summary_error", progress=1.0, message=f"Summary failed: {exc}")
        print(f"transcribe_recording: summary failed: {exc}", file=sys.stderr)
        return False, label


def write_manifest(job_dir: Path, recording: Path, level: str, transcriber_label: str | None,
                   summary_label: str | None, snippets: list[Path], extra: dict[str, Any] | None = None) -> None:
    settings = policy.job_settings(job_dir)
    hosts: dict[str, int] = {}
    for call in policy.calls(job_dir):
        hosts[call.get("host", "?")] = hosts.get(call.get("host", "?"), 0) + 1
    manifest = {
        "recording": str(recording),
        "sijainti": settings["sijainti"],
        "laatu": settings["laatu"],
        "taso": level,
        "transcribe_model": transcriber_label,
        "summary_model": summary_label,
        "kutsut_osoitteittain": hosts,
        "eu_ulkopuolella": policy.outside_eu(job_dir) if settings["sijainti"] == policy.EU else None,
        "snippets": [str(p) for p in snippets],
        "outputs": {p.name: str(p) for p in sorted(job_dir.glob("*.md")) + sorted(job_dir.glob("transcript.*"))},
    }
    if extra:
        manifest.update(extra)
    (job_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")


def finish_outputs(config: dict[str, Any], job_dir: Path) -> None:
    policy.stamp_outputs(job_dir)
    policy.report_violation(job_dir, config)


def run_perustaso(config: dict[str, Any], args: argparse.Namespace, recording: Path, job_dir: Path,
                  progress: Path, settings: dict[str, str]) -> int:
    requested_format = config["transcribe_output_format"]
    summary_enabled = (args.summary or config.get("summary", "on")) != "off"
    transcriber = build_transcriber(config, settings)
    if transcriber is None:
        message = "No transcription service available (no Gemini API key and no EU service)."
        write_progress(progress, stage="error", progress=1.0, message=message)
        print(f"transcribe_recording: {message}", file=sys.stderr)
        return 1
    if requested_format == "diarized_json":
        requested_format = "json"  # Google perustaso has no diarization; Huipputaso has speakers
    write_progress(progress, stage="starting", progress=0.02, message=f"Transcribing with {transcriber.model_label}")
    chunk_seconds = args.chunk_seconds or int(config.get("snippet_seconds", DEFAULT_CHUNK_SECONDS))
    try:
        snippets = split_snippets(
            recording, job_dir / "snippets", chunk_seconds, progress,
            max_seconds=config.get("snippet_max_seconds"),
            split_mode=str(config.get("snippet_split", "fixed")),
            window_seconds=int(config.get("snippet_silence_window_seconds", 120)),
            silence_db=float(config.get("snippet_silence_db", -35.0)),
        )
        silence_db = (float(config.get("silence_mean_db", DEFAULT_SILENCE_MEAN_DB))
                      if config.get("skip_silent_snippets", True) else None)
        text, chunks, diarized_chunks = transcribe_snippets(
            transcriber, snippets, job_dir, int(config.get("max_parallel_transcriptions", 3)), progress, silence_db,
        )
    except Exception as exc:  # noqa: BLE001 - record any failure so it can be retried
        write_progress(progress, stage="error", progress=1.0, message=f"Transcription failed: {exc}")
        print(f"transcribe_recording: transcription failed: {exc}", file=sys.stderr)
        finish_outputs(config, job_dir)
        policy.notify("Litterointi odottaa", f"{job_dir.name}: litterointi ei onnistunut, yritetään uudelleen.")
        return 1
    write_transcript_outputs(job_dir, requested_format, text, chunks, diarized_chunks)
    summary_ok, summary_label = True, None
    if summary_enabled:
        summary_ok, summary_label = summarize_safely(config, job_dir, text, progress, summary_options(config, args))
    huippu = settings["laatu"] == policy.HUIPPU
    write_manifest(job_dir, recording, "perustaso", transcriber.model_label, summary_label, snippets)
    finish_outputs(config, job_dir)
    if huippu:
        (job_dir / UPGRADE_QUEUE).write_text(
            json.dumps({"tila": "jonossa", "aika": policy.now_iso(), "yritykset": 0}, indent=2), encoding="utf-8")
    if not summary_ok:
        return 1
    message = "Perustaso valmis, Huipputaso jonossa" if huippu else "Done"
    write_progress(progress, stage="done", progress=1.0, message=message, total=len(snippets), current=len(snippets))
    return 0


def run_huipputaso(config: dict[str, Any], args: argparse.Namespace, recording: Path, job_dir: Path) -> int:
    """Background upgrade: replaces the perustaso outputs, which are kept as *-perustaso.*."""
    import max_mode

    progress = job_dir / UPGRADE_PROGRESS
    queue = job_dir / UPGRADE_QUEUE
    state = policy._read_json(queue)
    settings = policy.job_settings(job_dir)
    keep = ("transcript.md", "transcript.txt", "summary.md")
    for name in keep:  # keep the quick version (once; a retry must not overwrite it with a failed one)
        src, dst = job_dir / name, job_dir / name.replace(".", "-perustaso.", 1)
        if src.exists() and not dst.exists():
            shutil.copy2(src, dst)
    write_progress(progress, stage="starting", progress=0.01, message="Huipputaso starting")
    try:
        readable = max_mode.run(config, recording, job_dir, progress, use_transcribe=settings["sijainti"] == policy.GLOBAL)
    except Exception as exc:  # noqa: BLE001
        for name in keep:  # put the quick version back so the folder stays consistent
            src = job_dir / name.replace(".", "-perustaso.", 1)
            if src.exists():
                shutil.copy2(src, job_dir / name)
        state.update(tila="virhe", virhe=str(exc)[:300], yritykset=int(state.get("yritykset", 0)) + 1,
                     aika=policy.now_iso())
        queue.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
        write_progress(progress, stage="error", progress=1.0, message=f"Huipputaso failed: {exc}")
        print(f"transcribe_recording: Huipputaso failed: {exc}", file=sys.stderr)
        finish_outputs(config, job_dir)
        return 1
    summary_ok, summary_label = summarize_safely(config, job_dir, readable, progress, summary_options(config, args))
    write_manifest(job_dir, recording, "huipputaso", "huipputaso", summary_label,
                   sorted((job_dir / "snippets").glob("snippet-*.m4a")))
    finish_outputs(config, job_dir)
    queue.unlink(missing_ok=True)
    write_progress(progress, stage="done" if summary_ok else "summary_error", progress=1.0, message="Huipputaso valmis")
    policy.notify("Huipputaso valmis", f"{job_dir.name}: Huipputason litteraatti ja yhteenveto ovat valmiit.")
    return 0 if summary_ok else 1


def run_summary_only(config: dict[str, Any], args: argparse.Namespace, job_dir: Path, progress: Path) -> int:
    source = job_dir / ("transcript-luettava.md" if (job_dir / "transcript-luettava.md").exists() else "transcript.txt")
    if not source.exists():
        print("transcribe_recording: no transcript to summarize", file=sys.stderr)
        return 1
    text = source.read_text(encoding="utf-8")
    ok, _label = summarize_safely(config, job_dir, text, progress, summary_options(config, args))
    finish_outputs(config, job_dir)
    if ok:
        write_progress(progress, stage="done", progress=1.0, message="Summary retried: done")
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Chunk, transcribe, and summarize a meeting recording.")
    parser.add_argument("--recording", required=True)
    parser.add_argument("--config")
    parser.add_argument("--output-root", default="~/.meeting-transcriber/output")
    parser.add_argument("--format", choices=["txt", "md", "json", "diarized_json"])
    parser.add_argument("--summary", choices=["on", "off"])
    parser.add_argument("--summary-language", help='"auto" (match transcript) or a language name like "Finnish".')
    parser.add_argument("--chunk-seconds", type=int)
    parser.add_argument("--progress")
    parser.add_argument("--sijainti", choices=list(policy.SIJAINNIT),
                        help="Testaukseen: ohita kytkin (kirjataan työhön; EU voittaa silti).")
    parser.add_argument("--laatu", choices=list(policy.LAADUT), help="Testaukseen: ohita laatukytkin.")
    parser.add_argument("--huipputaso", action="store_true",
                        help="Aja jonossa oleva Huipputaso tälle palaverille (watcher käyttää tätä).")
    parser.add_argument("--vain-yhteenveto", action="store_true", help="Tee vain yhteenveto uudelleen.")
    args = parser.parse_args()

    config = load_config(Path(args.config).expanduser() if args.config else None)
    output_root = Path(args.output_root or config.get("output_dir", "~/.meeting-transcriber/output")).expanduser()
    job_dir, recording = ensure_job(Path(args.recording), output_root)
    progress = Path(args.progress).expanduser() if args.progress else output_paths(job_dir)["progress"]
    requested_format = args.format or config.get("transcribe_output_format") or "txt"
    config["transcribe_output_format"] = "txt" if requested_format == "text" else requested_format
    policy.set_call_log(job_dir)

    if args.huipputaso:
        return run_huipputaso(config, args, recording, job_dir)
    if args.vain_yhteenveto:
        return run_summary_only(config, args, job_dir, progress)

    switches = policy.read_switches(config)
    if args.sijainti:
        switches["sijainti"] = args.sijainti
    if args.laatu:
        switches["laatu"] = args.laatu
    settings = policy.lock_job(job_dir, switches, "litterointi")

    write_progress(progress, stage="starting", progress=0.01, message="Starting transcription")
    duration = recording_duration_seconds(recording)
    min_seconds = int(config.get("min_transcribe_seconds", DEFAULT_MIN_TRANSCRIBE_SECONDS))
    if duration is not None and duration < min_seconds:
        write_progress(progress, stage="skipped", progress=1.0, current=0, total=0,
                       message=f"Skipped transcription: recording is {duration:.1f}s, below {min_seconds}s minimum")
        return 0
    return run_perustaso(config, args, recording, job_dir, progress, policy.normalize(settings))


if __name__ == "__main__":
    raise SystemExit(main())
