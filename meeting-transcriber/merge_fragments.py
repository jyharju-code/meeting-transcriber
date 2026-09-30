#!/usr/bin/env python3
"""Merge a meeting that was recorded as several job folders into one document.

The watcher can split one meeting into several jobs when detection drops out
for a while. This tool stitches the per-job transcripts back together in time
order, marks every gap where no audio was recorded, and (optionally) writes one
fresh summary over the combined transcript with the configured summary provider.

    merge_fragments.py --jobs JOB [JOB ...] [--title T] [--out FILE] [--config CFG]
    merge_fragments.py --auto --since 20260930-1600 --until 20260930-1800 ...

`--auto` picks job folders from the output directory by folder timestamp.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import providers  # noqa: E402
import transcribe_recording as tr  # noqa: E402


def job_start(job_dir: Path) -> dt.datetime:
    stamp = "-".join(job_dir.name.split("-")[:2])
    return dt.datetime.strptime(stamp, "%Y%m%d-%H%M%S")


def recording_file(job_dir: Path) -> Path | None:
    for name in ("recording.mp4", "recording.m4a", "recording.wav"):
        if (job_dir / name).exists():
            return job_dir / name
    return None


def duration_seconds(path: Path | None) -> float:
    if path is None:
        return 0.0
    result = subprocess.run(
        [tr.ffprobe_path(), "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True,
    )
    try:
        return float(result.stdout.strip())
    except ValueError:
        return 0.0


def transcript_text(job_dir: Path) -> str:
    txt = job_dir / "transcript.txt"
    if txt.exists():
        return txt.read_text(encoding="utf-8").strip()
    md = job_dir / "transcript.md"
    if md.exists():
        return md.read_text(encoding="utf-8").replace("# Transcript", "", 1).strip()
    return ""


def meeting_title(job_dir: Path) -> str:
    meta = job_dir / "meeting.json"
    if meta.exists():
        try:
            return str(json.loads(meta.read_text(encoding="utf-8")).get("subject") or "")
        except (OSError, ValueError):
            return ""
    return ""


def fmt_clock(moment: dt.datetime) -> str:
    return moment.strftime("%H:%M:%S")


def fmt_span(seconds: float) -> str:
    seconds = int(round(seconds))
    return f"{seconds // 60} min {seconds % 60:02d} s"


def build_document(jobs: list[Path], title: str) -> tuple[str, str, dict[str, Any]]:
    """Return (markdown, plain combined transcript, stats)."""
    jobs = sorted(jobs, key=job_start)
    parts: list[str] = []
    plain: list[str] = []
    recorded = 0.0
    gaps: list[tuple[dt.datetime, dt.datetime]] = []
    previous_end: dt.datetime | None = None
    rows: list[str] = []

    for index, job in enumerate(jobs, start=1):
        start = job_start(job)
        length = duration_seconds(recording_file(job))
        end = start + dt.timedelta(seconds=length)
        recorded += length
        if previous_end is not None and start > previous_end:
            gap = (start - previous_end).total_seconds()
            gaps.append((previous_end, start))
            parts.append(
                f"> **[AUKKO {fmt_clock(previous_end)}-{fmt_clock(start)}, {fmt_span(gap)}: "
                "tältä ajalta ei tallentunut ääntä]**\n"
            )
        text = transcript_text(job) or "_(ei litteraattia)_"
        parts.append(f"### Osa {index} · {fmt_clock(start)}-{fmt_clock(end)} ({fmt_span(length)})\n\n{text}\n")
        plain.append(text)
        rows.append(f"| {index} | `{job.name}` | {fmt_clock(start)} | {fmt_clock(end)} | {fmt_span(length)} |")
        previous_end = end

    first = job_start(jobs[0])
    lost = sum((b - a).total_seconds() for a, b in gaps)
    stats = {"recorded": recorded, "lost_between": lost, "gaps": len(gaps),
             "first": first, "last": previous_end}

    header = [
        f"# {title}" if title else f"# Kokous {first:%d.%m.%Y}",
        "",
        f"**Päivä:** {first:%d.%m.%Y} · **Tallenne:** {fmt_clock(first)}-{fmt_clock(previous_end)} · "
        f"**Tallentunut ääni:** {fmt_span(recorded)} · **Aukkoja:** {len(gaps)} (yht. {fmt_span(lost)})",
        "",
        "> Koottu automaattisesti useasta tallennepätkästä aikajärjestyksessä. "
        "Aukkokohdissa ääntä ei tallentunut, joten niiden sisältö puuttuu.",
        "",
        "| Osa | Kansio | Alku | Loppu | Kesto |",
        "|---|---|---|---|---|",
        *rows,
        "",
    ]
    markdown = "\n".join(header) + "\n## Litteraatti\n\n" + "\n".join(parts)
    return markdown, "\n\n".join(plain), stats


def main() -> int:
    parser = argparse.ArgumentParser(description="Merge fragmented meeting jobs into one document.")
    parser.add_argument("--jobs", nargs="*", default=[])
    parser.add_argument("--auto", action="store_true", help="Select jobs by folder timestamp.")
    parser.add_argument("--since", help="YYYYMMDD-HHMM, inclusive (with --auto)")
    parser.add_argument("--until", help="YYYYMMDD-HHMM, inclusive (with --auto)")
    parser.add_argument("--output-root", default="~/.meeting-transcriber/output")
    parser.add_argument("--title", default="")
    parser.add_argument("--out", help="Output .md path (default: <first job>/merged.md)")
    parser.add_argument("--config", help="Config for the summary provider")
    parser.add_argument("--no-summary", action="store_true")
    args = parser.parse_args()

    jobs = [Path(j).expanduser() for j in args.jobs]
    if args.auto:
        root = Path(args.output_root).expanduser()
        since = dt.datetime.strptime(args.since, "%Y%m%d-%H%M") if args.since else dt.datetime.min
        until = dt.datetime.strptime(args.until, "%Y%m%d-%H%M") if args.until else dt.datetime.max
        for folder in sorted(root.iterdir()):
            try:
                start = job_start(folder)
            except ValueError:
                continue
            if folder.is_dir() and since <= start <= until and recording_file(folder):
                jobs.append(folder)
    jobs = [j for j in dict.fromkeys(jobs) if j.is_dir()]
    if not jobs:
        print("merge_fragments: no job folders selected", file=sys.stderr)
        return 1

    title = args.title or next((t for t in map(meeting_title, sorted(jobs, key=job_start)) if t), "")
    markdown, combined, stats = build_document(jobs, title)

    if not args.no_summary and combined.strip():
        config = tr.load_config(Path(args.config).expanduser() if args.config else None)
        summarizer = providers.select(
            providers.build_summarizers(config),
            config.get("summary_provider", "openai"),
            config.get("summary_fallback", []),
        )
        if summarizer is not None:
            prompt = tr.build_summary_prompt(
                combined,
                owner=str(config.get("meeting_owner", "") or ""),
                aliases=config.get("meeting_owner_aliases") or [],
                max_chars=int(config.get("summary_max_chars", tr.DEFAULT_SUMMARY_MAX_CHARS)),
                language=str(config.get("summary_language", "auto")),
            )
            summary = summarizer.summarize(prompt).strip()
            summary = "\n".join(
                ("#" + line) if line.startswith("#") else line for line in summary.splitlines()
            )  # demote headings one level under "## Yhteenveto"
            markdown = markdown.replace(
                "\n## Litteraatti\n",
                f"\n## Yhteenveto ({summarizer.model_label})\n\n"
                "> Huom: yhteenveto perustuu vain tallentuneisiin osiin.\n\n"
                f"{summary}\n\n## Litteraatti\n",
                1,
            )
        else:
            print("merge_fragments: no summary provider available; skipping summary", file=sys.stderr)

    out = Path(args.out).expanduser() if args.out else sorted(jobs, key=job_start)[0] / "merged.md"
    out.write_text(markdown.rstrip() + "\n", encoding="utf-8")
    print(f"merged {len(jobs)} jobs -> {out}")
    print(f"recorded {fmt_span(stats['recorded'])}, {stats['gaps']} gaps totalling {fmt_span(stats['lost_between'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
