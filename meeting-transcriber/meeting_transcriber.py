#!/usr/bin/env python3
"""Watch for Teams/Meet meetings, record audio, then transcribe it.

This script is intentionally local-first: it only starts recording when
`record_command` is configured, and it only transcribes when OPENAI_API_KEY is
available. See README.md for setup.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "config.json"
DEFAULT_OUTPUT = ROOT / "output"
TAB_DELIMITER = "|||MT_TAB|||"
MEET_RE = re.compile(r"https?://meet\.google\.com/[a-z]{3}-[a-z]{4}-[a-z]{3}", re.I)
TEAMS_URL_RE = re.compile(r"https?://(?:teams\.microsoft|teams\.live)\.com/", re.I)
TEAMS_WINDOW_RE = re.compile(r"\b(meeting|call|teams meeting)\b", re.I)


@dataclass
class Detection:
    provider: str
    source: str
    detail: str


class DashboardCommandProcess:
    def __init__(self, config: dict[str, Any], command_file: Path, command_id: str, audio_path: Path):
        self.config = config
        self.command_file = command_file
        self.command_id = command_id
        self.audio_path = audio_path

    def poll(self) -> None:
        return None

    def stop(self) -> None:
        write_dashboard_command(
            self.config,
            self.command_file,
            "stop",
            self.command_id,
            self.audio_path,
        )


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def log(config: dict[str, Any], message: str) -> None:
    stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] {message}"
    print(line, flush=True)

    log_file = Path(config.get("log_file", ROOT / "meeting-transcriber.log")).expanduser()
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with log_file.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def run_osascript(script: str) -> str:
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            check=False,
            capture_output=True,
            text=True,
            timeout=8,
        )
    except Exception:
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def app_is_running(process_name: str) -> bool:
    script = f'tell application "System Events" to exists process "{process_name}"'
    return run_osascript(script).lower() == "true"


def browser_tabs(app_name: str) -> list[tuple[str, str]]:
    if not app_is_running(app_name):
        return []

    if app_name == "Safari":
        script = f"""
set output to ""
tell application "Safari"
  repeat with w in windows
    repeat with t in tabs of w
      try
        set output to output & (URL of t) & "{TAB_DELIMITER}" & (name of t) & linefeed
      end try
    end repeat
  end repeat
end tell
return output
"""
    else:
        script = f"""
set output to ""
tell application "{app_name}"
  repeat with w in windows
    repeat with t in tabs of w
      try
        set output to output & (URL of t) & "{TAB_DELIMITER}" & (title of t) & linefeed
      end try
    end repeat
  end repeat
end tell
return output
"""

    rows: list[tuple[str, str]] = []
    for line in run_osascript(script).splitlines():
        if TAB_DELIMITER in line:
            url, title = line.split(TAB_DELIMITER, 1)
            rows.append((url.strip(), title.strip()))
    return rows


def teams_window_titles() -> list[str]:
    process_names = ["Microsoft Teams", "MSTeams", "Teams"]
    titles: list[str] = []
    for process_name in process_names:
        if not app_is_running(process_name):
            continue
        script = f"""
set output to ""
tell application "System Events"
  tell process "{process_name}"
    repeat with w in windows
      try
        set output to output & (name of w) & linefeed
      end try
    end repeat
  end tell
end tell
return output
"""
        titles.extend(title.strip() for title in run_osascript(script).splitlines() if title.strip())
    return titles


def detect_meeting(config: dict[str, Any]) -> Detection | None:
    browsers = config.get(
        "browser_apps",
        ["Google Chrome", "Microsoft Edge", "Brave Browser", "Arc", "Safari"],
    )

    for app_name in browsers:
        for url, title in browser_tabs(app_name):
            if MEET_RE.search(url):
                return Detection("Google Meet", app_name, title or url)
            if TEAMS_URL_RE.search(url) and ("meet" in url.lower() or "call" in title.lower()):
                return Detection("Microsoft Teams", app_name, title or url)

    for title in teams_window_titles():
        if TEAMS_WINDOW_RE.search(title):
            return Detection("Microsoft Teams", "Teams app", title)

    return None


def timestamp_slug() -> str:
    return dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def with_placeholders(command: list[str], output_path: Path, status_path: Path | None) -> list[str]:
    replacements = {
        "{output}": str(output_path),
        "{status}": str(status_path) if status_path else "",
    }
    full_command: list[str] = []
    for part in command:
        for token, value in replacements.items():
            part = part.replace(token, value)
        if part:
            full_command.append(part)
    return full_command


def has_job_artifacts(job_dir: Path) -> bool:
    artifact_patterns = [
        "recording.*",
        "transcript.*",
        "summary.md",
        "manifest.json",
    ]
    return any(any(job_dir.glob(pattern)) for pattern in artifact_patterns)


def sweep_orphan_job_folders(config: dict[str, Any]) -> None:
    output_dir = Path(config.get("output_dir", DEFAULT_OUTPUT)).expanduser()
    if not output_dir.exists():
        return
    min_age_seconds = int(config.get("orphan_job_min_age_seconds", 180))
    cutoff = time.time() - min_age_seconds
    for job_dir in output_dir.iterdir():
        if not job_dir.is_dir():
            continue
        try:
            modified = job_dir.stat().st_mtime
        except OSError:
            continue
        if modified > cutoff or has_job_artifacts(job_dir):
            continue
        try:
            shutil.rmtree(job_dir)
            log(config, f"Removed orphan recording folder with no recording or transcript: {job_dir}")
        except OSError as exc:
            log(config, f"Could not remove orphan recording folder {job_dir}: {exc}")


def default_dashboard_command_file() -> Path:
    return Path("~/.meeting-transcriber/dashboard-command.json").expanduser()


def ensure_dashboard_running(config: dict[str, Any]) -> None:
    app_path = Path(config.get("dashboard_app_path", "/Applications/Meeting Transcriber Dashboard.app")).expanduser()
    if not app_path.exists():
        log(config, f"Dashboard app not found: {app_path}")
        return
    subprocess.run(
        ["/usr/bin/open", "-gj", str(app_path)],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def write_dashboard_command(
    config: dict[str, Any],
    command_file: Path,
    command: str,
    command_id: str,
    audio_path: Path,
    detection: Detection | None = None,
) -> None:
    payload: dict[str, Any] = {
        "command": command,
        "id": command_id,
        "outputPath": str(audio_path),
        "createdAt": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    if detection:
        payload.update({
            "provider": detection.provider,
            "source": detection.source,
            "detail": detection.detail,
        })
    command_file.parent.mkdir(parents=True, exist_ok=True)
    temp_file = command_file.with_suffix(".tmp")
    temp_file.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    temp_file.replace(command_file)
    log(config, f"Dashboard command written: {command} {command_id}")


def start_recording(config: dict[str, Any], detection: Detection) -> tuple[subprocess.Popen[str] | DashboardCommandProcess | None, Path | None]:
    backend = str(config.get("recording_backend", "")).strip().lower()
    if backend == "dashboard_command":
        output_dir = Path(config.get("output_dir", DEFAULT_OUTPUT)).expanduser()
        output_dir.mkdir(parents=True, exist_ok=True)
        provider_slug = detection.provider.lower().replace(" ", "-")
        job_dir = output_dir / f"{timestamp_slug()}-{provider_slug}"
        job_dir.mkdir(parents=True, exist_ok=True)
        audio_path = job_dir / "recording.mp4"
        command_file = Path(config.get("dashboard_command_file", default_dashboard_command_file())).expanduser()
        ensure_dashboard_running(config)
        write_dashboard_command(config, command_file, "start", job_dir.name, audio_path, detection)
        log(config, f"Requested dashboard recording for {detection.provider}: {detection.detail}")
        return DashboardCommandProcess(config, command_file, job_dir.name, audio_path), audio_path

    command = config.get("record_command")
    if not command:
        log(config, f"Meeting detected but record_command is not configured: {detection}")
        return None, None
    if not isinstance(command, list) or not all(isinstance(part, str) for part in command):
        log(config, "record_command must be a JSON array of strings")
        return None, None

    output_dir = Path(config.get("output_dir", DEFAULT_OUTPUT)).expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)
    audio_ext = str(config.get("audio_extension", "wav")).lstrip(".")
    provider_slug = detection.provider.lower().replace(" ", "-")
    job_dir = output_dir / f"{timestamp_slug()}-{provider_slug}"
    job_dir.mkdir(parents=True, exist_ok=True)
    audio_path = job_dir / f"recording.{audio_ext}"
    status_path = Path(config["status_file"]).expanduser() if config.get("status_file") else None
    full_command = with_placeholders(command, audio_path, status_path)
    stdout_path = job_dir / "recorder.out.log"
    stderr_path = job_dir / "recorder.err.log"
    command_path = job_dir / "recorder-command.txt"
    command_path.write_text(" ".join(shlex.quote(part) for part in full_command) + "\n")

    log(config, f"Starting recording for {detection.provider}: {detection.detail}")
    try:
        stdout_file = stdout_path.open("w")
        stderr_file = stderr_path.open("w")
        proc = subprocess.Popen(
            full_command,
            stdout=stdout_file,
            stderr=stderr_file,
            text=True,
        )
        stdout_file.close()
        stderr_file.close()
    except FileNotFoundError as exc:
        log(config, f"Could not start recorder: {exc}")
        return None, None
    except Exception as exc:
        log(config, f"Could not start recorder: {exc}")
        return None, None

    return proc, audio_path


def log_recorder_failure(config: dict[str, Any], proc: subprocess.Popen[str], audio_path: Path | None) -> None:
    return_code = proc.poll()
    if return_code is None:
        return
    if not audio_path:
        log(config, f"Recorder exited unexpectedly with code {return_code}")
        return
    stderr_path = audio_path.parent / "recorder.err.log"
    stdout_path = audio_path.parent / "recorder.out.log"
    details = ""
    for path in (stderr_path, stdout_path):
        if not path.exists():
            continue
        text = path.read_text(errors="replace").strip()
        if text:
            details = text[-1200:]
            break
    suffix = f": {details}" if details else ""
    log(config, f"Recorder exited unexpectedly with code {return_code}{suffix}")


def stop_recording(config: dict[str, Any], proc: subprocess.Popen[str] | DashboardCommandProcess, grace_seconds: int) -> None:
    if isinstance(proc, DashboardCommandProcess):
        log(config, "Requesting dashboard to stop recording")
        proc.stop()
        time.sleep(min(2, grace_seconds))
        return
    if proc.poll() is not None:
        return
    log(config, "Stopping recording")
    proc.send_signal(signal.SIGINT)
    try:
        proc.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def transcribe(config: dict[str, Any], audio_path: Path) -> None:
    if not config.get("transcribe_after_recording", True):
        return
    if not audio_path.exists() or audio_path.stat().st_size == 0:
        log(config, f"Skipping transcription; audio file is missing or empty: {audio_path}")
        return
    if not os.environ.get("OPENAI_API_KEY"):
        log(config, "Skipping transcription; OPENAI_API_KEY is not set")
        return

    worker = Path(config.get("transcription_worker", ROOT / "transcribe_recording.py")).expanduser()
    if not worker.exists():
        log(config, f"Skipping transcription; worker not found: {worker}")
        return

    default_transcribe_python = Path("~/.meeting-transcriber/venv/bin/python").expanduser()
    transcribe_python = str(config.get("transcribe_python") or os.environ.get("TRANSCRIBE_PYTHON") or (default_transcribe_python if default_transcribe_python.exists() else sys.executable))

    cmd = [
        transcribe_python,
        str(worker),
        "--recording",
        str(audio_path),
        "--config",
        str(DEFAULT_CONFIG),
    ]
    log(config, f"Transcribing job in {audio_path.parent}")
    result = subprocess.run(cmd, check=False, capture_output=True, text=True)
    if result.returncode == 0:
        log(config, f"Transcript job saved: {audio_path.parent}")
    else:
        log(config, f"Transcription failed: {result.stderr.strip() or result.stdout.strip()}")


def watch(config_path: Path, once: bool = False) -> int:
    config = load_config(config_path)
    poll_seconds = int(config.get("poll_seconds", 10))
    start_after_hits = int(config.get("start_after_consecutive_detections", 2))
    stop_after_misses = int(config.get("stop_after_consecutive_misses", 4))
    max_minutes = int(config.get("max_recording_minutes", 180))
    stop_grace = int(config.get("recorder_stop_grace_seconds", 20))

    hits = 0
    misses = 0
    recorder: subprocess.Popen[str] | DashboardCommandProcess | None = None
    audio_path: Path | None = None
    started_at: float | None = None
    active_detection: Detection | None = None

    log(config, "Meeting transcriber watcher started")
    sweep_orphan_job_folders(config)
    while True:
        updated_config = load_config(config_path)
        if updated_config:
            config = updated_config
            poll_seconds = int(config.get("poll_seconds", poll_seconds))
            start_after_hits = int(config.get("start_after_consecutive_detections", start_after_hits))
            stop_after_misses = int(config.get("stop_after_consecutive_misses", stop_after_misses))
            max_minutes = int(config.get("max_recording_minutes", max_minutes))
            stop_grace = int(config.get("recorder_stop_grace_seconds", stop_grace))

        detection = detect_meeting(config)
        if once:
            log(config, f"Detection: {detection}" if detection else "Detection: none")
            return 0

        if detection:
            hits += 1
            misses = 0
        else:
            misses += 1
            hits = 0

        if recorder is None and detection and hits >= start_after_hits:
            active_detection = detection
            recorder, audio_path = start_recording(config, detection)
            started_at = time.time() if recorder else None

        if recorder is not None:
            too_long = started_at is not None and (time.time() - started_at) > max_minutes * 60
            if misses >= stop_after_misses or too_long or recorder.poll() is not None:
                dashboard_recording = isinstance(recorder, DashboardCommandProcess)
                if not dashboard_recording and recorder.poll() is not None:
                    log_recorder_failure(config, recorder, audio_path)
                stop_recording(config, recorder, stop_grace)
                finished_audio = audio_path
                recorder = None
                audio_path = None
                started_at = None
                active_detection = None
                hits = 0
                misses = 0
                if finished_audio and not dashboard_recording:
                    transcribe(config, finished_audio)

        time.sleep(poll_seconds)


def main() -> int:
    parser = argparse.ArgumentParser(description="Automatically record and transcribe Teams/Meet meetings.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--once", action="store_true", help="Run one detection pass and exit.")
    args = parser.parse_args()

    return watch(args.config, once=args.once)


if __name__ == "__main__":
    raise SystemExit(main())
