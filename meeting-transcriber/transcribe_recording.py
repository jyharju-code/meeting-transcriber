#!/usr/bin/env python3
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


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


def openai_client():
    from openai import OpenAI

    return OpenAI()


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


def split_snippets(recording: Path, snippets_dir: Path, chunk_seconds: int, progress: Path | None) -> list[Path]:
    snippets_dir.mkdir(parents=True, exist_ok=True)
    existing = sorted(snippets_dir.glob("snippet-*.m4a"))
    if existing:
        return existing

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


def transcribe_snippets(
    client: Any,
    snippets: list[Path],
    job_dir: Path,
    requested_format: str,
    transcribe_model: str,
    diarize_model: str,
    max_parallel: int,
    progress: Path | None,
) -> tuple[str, list[dict[str, Any]], list[Any]]:
    snippet_transcript_dir = job_dir / "snippets" / "transcripts"
    snippet_transcript_dir.mkdir(parents=True, exist_ok=True)
    text_parts: list[str] = []
    json_chunks: list[dict[str, Any]] = []
    diarized_chunks: list[Any] = []

    use_diarized = requested_format == "diarized_json"
    model = diarize_model if use_diarized else transcribe_model
    response_format = "diarized_json" if use_diarized else "json"

    def transcribe_one(index: int, snippet: Path) -> dict[str, Any]:
        with snippet.open("rb") as audio_file:
            result = client.audio.transcriptions.create(
                file=audio_file,
                model=model,
                response_format=response_format,
        )
        data = result_jsonable(result)
        text = transcript_text(result)
        payload: dict[str, Any] = {
            "index": index,
            "file": str(snippet),
            "model": model,
            "text": text,
            "raw": data,
        }
        (snippet_transcript_dir / f"{snippet.stem}.json").write_text(
            json.dumps(payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return payload

    payloads: dict[int, dict[str, Any]] = {}
    completed = 0
    with ThreadPoolExecutor(max_workers=max(1, max_parallel)) as pool:
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

    return f"""
{intro}

Create concise Markdown meeting notes from this transcript.

Rules:
- Put ACTION ITEMS first.
- Action items must be grouped by person when a responsible person can be inferred.{owner_rules}
- If ownership is unclear, put it under "Unassigned".
- After action items, include Decisions, Key Points, Risks/Open Questions, and Short Summary.
- Do not invent facts not supported by the transcript.

Transcript:
{text[:max_chars]}
""".strip()


def summarize(
    client: Any,
    job_dir: Path,
    text: str,
    summary_model: str,
    progress: Path | None,
    owner: str = "",
    aliases: list[str] | None = None,
    max_chars: int = DEFAULT_SUMMARY_MAX_CHARS,
) -> None:
    paths = output_paths(job_dir)
    write_progress(progress, stage="summarizing", progress=0.84, message="Creating summary and action items")
    if not text.strip():
        paths["summary"].write_text("# Summary\n\nNo transcript text was available.\n", encoding="utf-8")
        return

    if len(text) > max_chars:
        print(
            f"transcribe_recording: transcript is {len(text)} chars; "
            f"truncating to {max_chars} for the summary prompt.",
            file=sys.stderr,
        )

    prompt = build_summary_prompt(text, owner=owner, aliases=aliases, max_chars=max_chars)

    response = client.responses.create(
        model=summary_model,
        input=prompt,
    )
    summary = getattr(response, "output_text", None) or str(response)
    if not summary.lstrip().startswith("#"):
        summary = "# Meeting Summary\n\n" + summary
    paths["summary"].write_text(summary.strip() + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Chunk, transcribe, and summarize a meeting recording.")
    parser.add_argument("--recording", required=True)
    parser.add_argument("--config")
    parser.add_argument("--output-root", default="~/.meeting-transcriber/output")
    parser.add_argument("--format", choices=["txt", "md", "json", "diarized_json"])
    parser.add_argument("--transcribe-model")
    parser.add_argument("--diarize-model")
    parser.add_argument("--summary-model")
    parser.add_argument("--summary", choices=["on", "off"])
    parser.add_argument("--chunk-seconds", type=int)
    parser.add_argument("--progress")
    args = parser.parse_args()

    config = load_config(Path(args.config).expanduser() if args.config else None)
    output_root = Path(args.output_root or config.get("output_dir", "~/.meeting-transcriber/output")).expanduser()
    job_dir, recording = ensure_job(Path(args.recording), output_root)
    paths = output_paths(job_dir)
    progress = Path(args.progress).expanduser() if args.progress else paths["progress"]

    requested_format = args.format or config.get("transcribe_output_format") or "txt"
    if requested_format == "text":
        requested_format = "txt"
    transcribe_model = args.transcribe_model or config.get("transcribe_model") or DEFAULT_TRANSCRIBE_MODEL
    diarize_model = args.diarize_model or config.get("diarize_model") or DEFAULT_DIARIZE_MODEL
    summary_model = args.summary_model or config.get("summary_model") or DEFAULT_SUMMARY_MODEL
    summary_enabled = (args.summary or config.get("summary", "on")) != "off"
    chunk_seconds = args.chunk_seconds or int(config.get("snippet_seconds", DEFAULT_CHUNK_SECONDS))
    max_parallel = int(config.get("max_parallel_transcriptions", 3))
    min_transcribe_seconds = int(config.get("min_transcribe_seconds", DEFAULT_MIN_TRANSCRIBE_SECONDS))
    summary_owner = str(config.get("meeting_owner", "") or "")
    summary_aliases = config.get("meeting_owner_aliases") or []
    if not isinstance(summary_aliases, list):
        summary_aliases = []
    summary_max_chars = int(config.get("summary_max_chars", DEFAULT_SUMMARY_MAX_CHARS))

    write_progress(progress, stage="starting", progress=0.01, message="Starting transcription")
    duration = recording_duration_seconds(recording)
    if duration is not None and duration < min_transcribe_seconds:
        write_progress(
            progress,
            stage="skipped",
            progress=1.0,
            message=f"Skipped transcription: recording is {duration:.1f}s, below {min_transcribe_seconds}s minimum",
            current=0,
            total=0,
        )
        return 0

    client = openai_client()
    snippets = split_snippets(recording, job_dir / "snippets", chunk_seconds, progress)
    text, chunks, diarized_chunks = transcribe_snippets(
        client,
        snippets,
        job_dir,
        requested_format,
        transcribe_model,
        diarize_model,
        max_parallel,
        progress,
    )
    write_transcript_outputs(job_dir, requested_format, text, chunks, diarized_chunks)
    if summary_enabled:
        summarize(
            client,
            job_dir,
            text,
            summary_model,
            progress,
            owner=summary_owner,
            aliases=summary_aliases,
            max_chars=summary_max_chars,
        )

    manifest = {
        "recording": str(recording),
        "format": requested_format,
        "transcribe_model": diarize_model if requested_format == "diarized_json" else transcribe_model,
        "summary_model": summary_model if summary_enabled else None,
        "summary": summary_enabled,
        "snippets": [str(path) for path in snippets],
        "outputs": {name: str(path) for name, path in paths.items() if path.exists()},
    }
    paths["manifest"].write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    write_progress(progress, stage="done", progress=1.0, message="Done", total=len(snippets), current=len(snippets))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
