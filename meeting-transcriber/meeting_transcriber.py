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

import policy


ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "config.json"
DEFAULT_OUTPUT = ROOT / "output"
TAB_DELIMITER = "|||MT_TAB|||"
MEET_RE = re.compile(r"https?://meet\.google\.com/[a-z]{3}-[a-z]{4}-[a-z]{3}", re.I)
TEAMS_URL_RE = re.compile(r"https?://(?:teams\.microsoft|teams\.live)\.com/", re.I)
TEAMS_WINDOW_RE = re.compile(r"\b(meeting|call|teams meeting)\b", re.I)
DEFAULT_TEAMS_IGNORED_TITLES = [
    "liity keskusteluun",
    "join the conversation",
    "join now",
    "pre-join",
]
# Teams (new client) only puts the word "Meeting" in a window title for the
# pre-join screen ("Meeting join | <subject> | ...") and the pop-out compact view
# ("Meeting compact view | <subject> | ..."). The full meeting window is titled
# just "<subject> | ... | Microsoft Teams", so a title regex alone loses the
# meeting whenever the user focuses Teams. We therefore learn the meeting subject
# and keep matching any Teams window that carries it.
TEAMS_MEETING_PREFIX_RE = re.compile(
    r"^(meeting(\s+(compact view|join|window|stage))?|call|kokous(\s+\S+)*|puhelu)$", re.I
)
TEAMS_TITLE_NOISE_RE = re.compile(
    r"^(microsoft teams|personal|work or school|anonymous|henkilökohtainen|työ tai koulu|\S+@\S+\.\S+)$",
    re.I,
)
TEAMS_NAV_SECTIONS = {
    "chat", "activity", "calendar", "teams", "files", "calls", "apps", "communities",
    "onedrive", "copilot", "planner", "settings", "people",
    "keskustelu", "keskustelut", "toiminta", "kalenteri", "tiedostot", "puhelut",
    "sovellukset", "yhteisöt", "asetukset", "henkilöt",
}
TEAMS_GENERIC_SUBJECTS = {
    "microsoft teams meeting", "teams meeting", "meeting", "kokous",
    "microsoft teams -kokous", "teams-kokous",
}
STICKY_SOURCE = "Teams app (meeting window)"


@dataclass
class Detection:
    provider: str
    source: str
    detail: str
    subject: str = ""


def teams_title_segments(title: str) -> list[str]:
    return [part.strip() for part in title.split("|") if part.strip()]


def teams_meeting_subject(title: str) -> str:
    """Extract the meeting subject from a Teams window title, or "" if unknown."""
    segments = teams_title_segments(title)
    if segments and TEAMS_MEETING_PREFIX_RE.match(segments[0]):
        segments = segments[1:]
    segments = [s for s in segments if not TEAMS_TITLE_NOISE_RE.match(s)]
    if not segments:
        return ""
    subject = segments[0]
    if len(subject) < 4 or subject.casefold() in TEAMS_GENERIC_SUBJECTS:
        return ""
    if subject.casefold() in TEAMS_NAV_SECTIONS:
        return ""
    return subject


def teams_title_is_nav(title: str) -> bool:
    """True for Teams navigation windows (Chat | ..., Calendar | ...), never a call."""
    segments = teams_title_segments(title)
    return bool(segments) and segments[0].casefold() in TEAMS_NAV_SECTIONS


def sticky_teams_detection(titles: list[str], subjects: list[str]) -> Detection | None:
    for subject in subjects:
        needle = subject.casefold()
        for title in titles:
            if needle in title.casefold() and not teams_title_is_nav(title):
                return Detection("Microsoft Teams", STICKY_SOURCE, title, subject)
    return None


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


@dataclass
class ActiveRecording:
    recorder: subprocess.Popen[str] | DashboardCommandProcess | None = None
    audio_path: Path | None = None
    started_at: float | None = None
    detection: Detection | None = None

    def clear(self) -> None:
        self.recorder = None
        self.audio_path = None
        self.started_at = None
        self.detection = None


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


OSASCRIPT_ERRORS: dict[str, float] = {}  # error -> last time logged (rate limit)
OSASCRIPT_LOG: dict[str, Any] = {"config": None}


def note_osascript_error(label: str, detail: str) -> None:
    """Log a failed AppleScript call at most once an hour per error, so a silent detection gap
    (missing Automation permission, a hung browser) shows up in the log."""
    key = f"{label}: {detail[:160]}"
    now = time.time()
    if now - OSASCRIPT_ERRORS.get(key, 0) < 3600:
        return
    OSASCRIPT_ERRORS[key] = now
    if OSASCRIPT_LOG["config"] is not None:
        log(OSASCRIPT_LOG["config"], f"Detection cannot read {key}")


def run_osascript(script: str, label: str = "osascript") -> str:
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except subprocess.TimeoutExpired:
        note_osascript_error(label, "timed out after 20 s")
        return ""
    except Exception as exc:  # noqa: BLE001
        note_osascript_error(label, type(exc).__name__)
        return ""
    if result.returncode != 0:
        note_osascript_error(label, (result.stderr or f"exit {result.returncode}").strip())
        return ""
    return result.stdout.strip()


def app_is_running(process_name: str) -> bool:
    script = f'tell application "System Events" to exists process "{process_name}"'
    return run_osascript(script).lower() == "true"


def browser_tabs(app_name: str) -> list[tuple[str, str]]:
    if not app_is_running(app_name):
        return []

    # Two bulk Apple events instead of two per tab: a busy browser (a Meet call running) answers
    # the per-tab loop too slowly, the query timed out and the meeting was not detected.
    name_property = "name" if app_name == "Safari" else "title"
    script = f"""
set output to ""
tell application "{app_name}"
  set allUrls to URL of every tab of every window
  set allTitles to {name_property} of every tab of every window
end tell
repeat with i from 1 to count of allUrls
  set windowUrls to item i of allUrls
  set windowTitles to item i of allTitles
  repeat with j from 1 to count of windowUrls
    try
      set output to output & (item j of windowUrls) & "{TAB_DELIMITER}" & (item j of windowTitles) & linefeed
    end try
  end repeat
end repeat
return output
"""

    rows: list[tuple[str, str]] = []
    for line in run_osascript(script, app_name).splitlines():
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


def detect_meeting(config: dict[str, Any], sticky_subjects: list[str] | None = None) -> Detection | None:
    browsers = config.get(
        "browser_apps",
        ["Google Chrome", "Microsoft Edge", "Brave Browser", "Arc", "Safari"],
    )

    for app_name in browsers:
        for url, title in browser_tabs(app_name):
            if MEET_RE.search(url):
                return Detection("Google Meet", app_name, title or url)
            if (
                TEAMS_URL_RE.search(url)
                and ("meet" in url.lower() or "call" in title.lower())
                and not teams_title_is_ignored(title, config)
            ):
                return Detection("Microsoft Teams", app_name, title or url, teams_meeting_subject(title))

    titles = teams_window_titles()
    for title in titles:
        if TEAMS_WINDOW_RE.search(title):
            return Detection("Microsoft Teams", "Teams app", title, teams_meeting_subject(title))

    # The meeting window itself has no "Meeting" in its title; recognise it by the
    # subject learned from the join screen / compact view.
    if sticky_subjects:
        return sticky_teams_detection(titles, sticky_subjects)

    return None


def teams_title_is_ignored(title: str, config: dict[str, Any]) -> bool:
    ignored = config.get("teams_ignored_titles", DEFAULT_TEAMS_IGNORED_TITLES)
    if not isinstance(ignored, list):
        ignored = DEFAULT_TEAMS_IGNORED_TITLES
    normalized = title.casefold().strip()
    return any(str(value).casefold().strip() in normalized for value in ignored if str(value).strip())


def timestamp_slug() -> str:
    return dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def with_placeholders(command: list[str], output_path: Path, status_path: Path | None) -> list[str]:
    replacements = {
        "{output}": str(output_path),
        "{status}": str(status_path) if status_path else "",
    }
    full_command: list[str] = []
    for part in command:
        had_placeholder = any(token in part for token in replacements)
        for token, value in replacements.items():
            part = part.replace(token, value)
        if part == "":
            # A placeholder that resolves to nothing (e.g. no status_file configured)
            # would otherwise leave its preceding option flag dangling without a value.
            if had_placeholder and full_command and full_command[-1].startswith("-"):
                full_command.pop()
            continue
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


def control_file(config: dict[str, Any], key: str, default: str) -> Path:
    return Path(config.get(key, default)).expanduser()


def read_json_object(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else None


def read_auto_suppression(config: dict[str, Any]) -> dict[str, Any] | None:
    path = control_file(
        config,
        "auto_suppression_file",
        "~/.meeting-transcriber/auto-suppression.json",
    )
    if not path.exists():
        return None
    try:
        payload = read_json_object(path)
    except (OSError, json.JSONDecodeError) as exc:
        log(config, f"Automatic recording remains suppressed; invalid suppression file: {exc}")
        return {"suppressed": True, "malformed": True}
    if payload is None:
        log(config, "Automatic recording remains suppressed; suppression file is not a JSON object")
        return {"suppressed": True, "malformed": True}
    return payload if payload.get("suppressed") is True else None


def clear_auto_suppression(config: dict[str, Any]) -> None:
    path = control_file(
        config,
        "auto_suppression_file",
        "~/.meeting-transcriber/auto-suppression.json",
    )
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        log(config, f"Could not clear automatic recording suppression: {exc}")
        return
    log(config, "Automatic recording suppression cleared after meeting detection ended")


def read_dashboard_ack(config: dict[str, Any]) -> dict[str, Any] | None:
    path = control_file(
        config,
        "dashboard_ack_file",
        "~/.meeting-transcriber/dashboard-ack.json",
    )
    try:
        return read_json_object(path)
    except (OSError, json.JSONDecodeError) as exc:
        log(config, f"Ignoring invalid dashboard acknowledgement: {exc}")
        return None


def acknowledgement_finishes_session(
    session: ActiveRecording,
    acknowledgement: dict[str, Any] | None,
) -> bool:
    if not isinstance(session.recorder, DashboardCommandProcess) or not acknowledgement:
        return False
    if acknowledgement.get("commandID") != session.recorder.command_id:
        return False
    if acknowledgement.get("state") not in {"stopped", "failed"}:
        return False
    session.clear()
    return True


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


def write_meeting_meta(job_dir: Path, detection: Detection) -> None:
    """Record what was detected so fragments of one meeting can be regrouped."""
    try:
        (job_dir / "meeting.json").write_text(
            json.dumps(
                {
                    "provider": detection.provider,
                    "source": detection.source,
                    "detail": detection.detail,
                    "subject": detection.subject,
                    "started_at": dt.datetime.now().isoformat(timespec="seconds"),
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except OSError:
        pass


def start_recording(config: dict[str, Any], detection: Detection) -> tuple[subprocess.Popen[str] | DashboardCommandProcess | None, Path | None]:
    backend = str(config.get("recording_backend", "")).strip().lower()
    if backend == "dashboard_command":
        output_dir = Path(config.get("output_dir", DEFAULT_OUTPUT)).expanduser()
        output_dir.mkdir(parents=True, exist_ok=True)
        provider_slug = detection.provider.lower().replace(" ", "-")
        job_dir = output_dir / f"{timestamp_slug()}-{provider_slug}"
        job_dir.mkdir(parents=True, exist_ok=True)
        audio_path = job_dir / "recording.mp4"
        write_meeting_meta(job_dir, detection)
        policy.lock_job(job_dir, policy.read_switches(config), "nauhoitus")
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
    write_meeting_meta(job_dir, detection)
    policy.lock_job(job_dir, policy.read_switches(config), "nauhoitus")
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


API_KEY_ENVS = ("GEMINI_API_KEY", "GOOGLE_API_KEY")
RECORDING_NAMES = ("recording.mp4", "recording.m4a", "recording.wav")


def worker_command(config: dict[str, Any], audio_path: Path) -> list[str] | None:
    """Build the transcription worker command, or None (with a log line) if it cannot run."""
    if not any(os.environ.get(name) for name in API_KEY_ENVS) and not config.get("agent_platform_project"):
        log(config, "Skipping transcription; no transcription API key is set")
        return None
    worker = Path(config.get("transcription_worker", ROOT / "transcribe_recording.py")).expanduser()
    if not worker.exists():
        log(config, f"Skipping transcription; worker not found: {worker}")
        return None
    default_python = Path("~/.meeting-transcriber/venv/bin/python").expanduser()
    python = str(
        config.get("transcribe_python")
        or os.environ.get("TRANSCRIBE_PYTHON")
        or (default_python if default_python.exists() else sys.executable)
    )
    return [str(Path(python).expanduser()), str(worker), "--recording", str(audio_path), "--config", str(DEFAULT_CONFIG)]


# --------------------------------------------------------------------------- #
# Catch-up transcription
#
# The dashboard only starts the worker when a recording ends cleanly. If the
# capture stream stops with an error (screen lock, sleep, app restart) or the
# dashboard quits while the worker runs, a perfectly usable recording is left
# without a transcript. The watcher therefore sweeps for such jobs and
# transcribes them in the background.
# --------------------------------------------------------------------------- #

def job_recording(job_dir: Path) -> Path | None:
    for name in RECORDING_NAMES:
        path = job_dir / name
        if path.exists() and path.stat().st_size > 0:
            return path
    return None


CATCHUP_ATTEMPTS_FILE = "catchup-attempts.txt"
SUMMARY_ATTEMPTS_FILE = "yhteenveto-attempts.txt"
UPGRADE_QUEUE = "huipputaso-jono.json"
# Minutes to wait before attempt n+1 after n failed attempts: growing delay for about a day (D3).
RETRY_MINUTES = (5, 10, 20, 40, 80, 160, 240, 240, 240, 240, 240)


def retry_due(marker: Path, attempts: int, now: float) -> bool:
    """True when a job that failed `attempts` times may be tried again."""
    if attempts <= 0:
        return True
    if attempts > len(RETRY_MINUTES):
        return False
    try:
        return now - marker.stat().st_mtime >= RETRY_MINUTES[attempts - 1] * 60
    except OSError:
        return True


def read_count(path: Path) -> int:
    try:
        return int(path.read_text().strip() or 0)
    except (OSError, ValueError):
        return 0


def catchup_attempts(job_dir: Path) -> int:
    try:
        return int((job_dir / CATCHUP_ATTEMPTS_FILE).read_text().strip() or 0)
    except (OSError, ValueError):
        return 0


def job_needs_transcription(
    job_dir: Path,
    now: float,
    min_age_s: float,
    max_age_s: float,
    retry_after_s: float | None = None,
    max_attempts: int | None = None,
) -> Path | None:
    """Return the recording of a job that still needs a transcript, else None.

    Never-attempted jobs qualify at once. A failed attempt (stage "error", or a
    progress file that stopped updating, e.g. the spend cap was hit or the worker
    died) is retried every `retry_after_s`, at most `max_attempts` times.
    """
    recording = job_recording(job_dir)
    if recording is None or any(job_dir.glob("transcript.*")):
        return None
    progress = job_dir / "progress.json"
    if progress.exists():
        state = read_json_object(progress) or {}
        if state.get("stage") in ("done", "skipped"):
            return None
        attempts = catchup_attempts(job_dir)
        if retry_after_s is None:
            if not retry_due(progress, max(attempts, 1), now):
                return None  # running, or failed too recently, or gave up after about a day
        else:
            if now - progress.stat().st_mtime < retry_after_s:
                return None
            if attempts >= (max_attempts or 6):
                return None
    age = now - recording.stat().st_mtime
    if age < min_age_s or age > max_age_s:
        return None
    return recording


def dashboard_is_recording(config: dict[str, Any], now: float, stale_after_s: float = 60) -> bool:
    status_path = Path(config.get("status_file", "~/.meeting-transcriber/status.json")).expanduser()
    status = read_json_object(status_path)
    if not status or not status.get("recording"):
        return False
    try:
        return now - status_path.stat().st_mtime <= stale_after_s
    except OSError:
        return False


def find_catchup_job(config: dict[str, Any], now: float, exclude: Path | None = None) -> Path | None:
    output_dir = Path(config.get("output_dir", DEFAULT_OUTPUT)).expanduser()
    if not output_dir.exists():
        return None
    min_age = float(config.get("catchup_min_age_minutes", 10)) * 60
    max_age = float(config.get("catchup_max_age_hours", 48)) * 3600
    for job_dir in sorted((p for p in output_dir.iterdir() if p.is_dir()), reverse=True):
        if exclude is not None and job_dir == exclude:
            continue
        recording = job_needs_transcription(job_dir, now, min_age, max_age)
        if recording is not None:
            return recording
    return None


def job_needs_summary(job_dir: Path, now: float, max_age_s: float) -> Path | None:
    """A transcript exists but its summary failed (stage summary_error): retry the summary alone."""
    recording = job_recording(job_dir)
    if recording is None or now - recording.stat().st_mtime > max_age_s:
        return None
    for name in ("progress-huipputaso.json", "progress.json"):
        progress = job_dir / name
        state = read_json_object(progress) or {}
        if state.get("stage") == "summary_error":
            attempts = read_count(job_dir / SUMMARY_ATTEMPTS_FILE)
            return recording if retry_due(progress, attempts, now) else None
        if progress.exists():
            return None
    return None


def job_needs_upgrade(job_dir: Path, now: float, max_age_s: float) -> Path | None:
    """Huipputaso is queued (or failed earlier and is due for a retry) and perustaso is finished."""
    queue = job_dir / UPGRADE_QUEUE
    recording = job_recording(job_dir)
    if recording is None or not queue.exists() or now - recording.stat().st_mtime > max_age_s:
        return None
    base = (read_json_object(job_dir / "progress.json") or {}).get("stage")
    if base not in ("done", "summary_error"):
        return None
    state = read_json_object(queue) or {}
    attempts = int(state.get("yritykset", 0) or 0)
    return recording if retry_due(queue, attempts, now) else None


def find_work(config: dict[str, Any], now: float) -> tuple[str, Path] | None:
    """Next background job: untranscribed recordings first, then summaries, then Huipputaso."""
    pending = find_catchup_job(config, now)
    if pending is not None:
        return "litterointi", pending
    output_dir = Path(config.get("output_dir", DEFAULT_OUTPUT)).expanduser()
    if not output_dir.exists():
        return None
    max_age = float(config.get("catchup_max_age_hours", 48)) * 3600
    jobs = sorted((p for p in output_dir.iterdir() if p.is_dir()), reverse=True)
    for kind, check in (("yhteenveto", job_needs_summary), ("huipputaso", job_needs_upgrade)):
        for job_dir in jobs:
            recording = check(job_dir, now, max_age)
            if recording is not None:
                return kind, recording
    return None


def start_catchup(config: dict[str, Any], audio_path: Path, kind: str = "litterointi") -> subprocess.Popen[str] | None:
    cmd = worker_command(config, audio_path)
    if cmd is None:
        return None
    job_dir = audio_path.parent
    log_path = job_dir / {"litterointi": "catchup-transcription.log", "yhteenveto": "yhteenveto-uusinta.log",
                          "huipputaso": "huipputaso.log"}[kind]
    if kind == "yhteenveto":
        cmd.append("--vain-yhteenveto")
    elif kind == "huipputaso":
        cmd.append("--huipputaso")
    try:
        if kind == "litterointi":
            (job_dir / CATCHUP_ATTEMPTS_FILE).write_text(str(catchup_attempts(job_dir) + 1))
        elif kind == "yhteenveto":
            (job_dir / SUMMARY_ATTEMPTS_FILE).write_text(str(read_count(job_dir / SUMMARY_ATTEMPTS_FILE) + 1))
        with log_path.open("w", encoding="utf-8") as handle:
            return subprocess.Popen(cmd, stdout=handle, stderr=subprocess.STDOUT, text=True)
    except OSError as exc:
        log(config, f"Could not start catch-up transcription for {audio_path.parent}: {exc}")
        return None


def notify_failure(config: dict[str, Any], kind: str, job_dir: Path) -> None:
    """One notification per job and kind, on the first failure (later retries stay quiet)."""
    marker = job_dir / f".ilmoitettu-{kind}"
    if marker.exists():
        return
    try:
        marker.write_text(policy.now_iso())
    except OSError:
        pass
    what = {"litterointi": "litterointi", "yhteenveto": "yhteenveto", "huipputaso": "Huipputaso"}.get(kind, kind)
    policy.notify(f"{what.capitalize()} odottaa",
                  f"{job_dir.name}: {what} ei onnistunut. Yritetään uudelleen automaattisesti vuorokauden ajan.")


def transcribe(config: dict[str, Any], audio_path: Path) -> None:
    if not config.get("transcribe_after_recording", True):
        return
    if not audio_path.exists() or audio_path.stat().st_size == 0:
        log(config, f"Skipping transcription; audio file is missing or empty: {audio_path}")
        return
    cmd = worker_command(config, audio_path)
    if cmd is None:
        return
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
    recording_stop_misses = int(config.get("stop_after_consecutive_misses_while_recording", 18))
    subject_memory = int(config.get("teams_subject_memory_seconds", 300))
    max_minutes = int(config.get("max_recording_minutes", 180))
    stop_grace = int(config.get("recorder_stop_grace_seconds", 20))

    hits = 0
    misses = 0
    session = ActiveRecording()
    suppression_misses = 0
    recent_subject = ""
    recent_subject_at = 0.0
    was_sticky = False
    last_titles: list[str] = []
    catchup_proc: subprocess.Popen[str] | None = None
    catchup_job: Path | None = None
    catchup_kind = ""
    last_catchup_scan = 0.0
    last_login_check = 0.0

    log(config, "Meeting transcriber watcher started")
    OSASCRIPT_LOG["config"] = config
    sweep_orphan_job_folders(config)
    while True:
        updated_config = load_config(config_path)
        if updated_config:
            config = updated_config
            poll_seconds = int(config.get("poll_seconds", poll_seconds))
            start_after_hits = int(config.get("start_after_consecutive_detections", start_after_hits))
            stop_after_misses = int(config.get("stop_after_consecutive_misses", stop_after_misses))
            recording_stop_misses = int(
                config.get("stop_after_consecutive_misses_while_recording", recording_stop_misses)
            )
            subject_memory = int(config.get("teams_subject_memory_seconds", subject_memory))
            max_minutes = int(config.get("max_recording_minutes", max_minutes))
            stop_grace = int(config.get("recorder_stop_grace_seconds", stop_grace))

        if config.get("log_teams_window_titles"):
            current_titles = teams_window_titles()
            if current_titles != last_titles:
                log(config, "Teams windows: " + ("; ".join(current_titles) or "(none)"))
                last_titles = current_titles

        sticky: list[str] = []
        if session.detection is not None and session.detection.subject:
            sticky.append(session.detection.subject)
        if recent_subject and time.time() - recent_subject_at <= subject_memory and recent_subject not in sticky:
            sticky.append(recent_subject)

        detection = detect_meeting(config, sticky)
        if once:
            log(config, f"Detection: {detection}" if detection else "Detection: none")
            return 0

        meeting_switched = False
        if detection is not None and detection.subject and detection.source != STICKY_SOURCE:
            recent_subject, recent_subject_at = detection.subject, time.time()
            current = session.detection
            if session.recorder is not None and current is not None:
                if not current.subject:
                    current.subject = detection.subject
                elif detection.subject.casefold() != current.subject.casefold():
                    meeting_switched = True
                    log(config, f"Different meeting detected ('{detection.subject}'); closing '{current.subject}'")

        if session.recorder is not None:
            now_sticky = detection is not None and detection.source == STICKY_SOURCE
            if now_sticky and not was_sticky:
                log(config, f"Meeting window in focus; keeping recording alive for '{detection.subject}'")
            was_sticky = now_sticky
            if detection is None and misses == 0:
                titles = "; ".join(teams_window_titles()[:6]) or "(no Teams windows)"
                log(config, f"Meeting not detected while recording; Teams windows: {titles}")

        suppression = read_auto_suppression(config)
        acknowledgement = read_dashboard_ack(config)
        if acknowledgement_finishes_session(session, acknowledgement):
            log(config, "Dashboard confirmed that the automatic recording ended")
            hits = 0
            misses = 0

        if suppression:
            hits = 0
            misses = 0
            if detection:
                suppression_misses = 0
            else:
                suppression_misses += 1
                if suppression_misses >= stop_after_misses:
                    clear_auto_suppression(config)
                    suppression = None
                    suppression_misses = 0
        else:
            suppression_misses = 0
            if detection:
                hits += 1
                misses = 0
            else:
                misses += 1
                hits = 0

        if session.recorder is None and detection and hits >= start_after_hits and not suppression:
            session.detection = detection
            session.recorder, session.audio_path = start_recording(config, detection)
            session.started_at = time.time() if session.recorder else None

        if session.recorder is not None:
            too_long = session.started_at is not None and (time.time() - session.started_at) > max_minutes * 60
            # A short focus change must never split a meeting: while recording we wait
            # `stop_after_consecutive_misses_while_recording` polls (default 3 min).
            lost = misses >= recording_stop_misses
            if lost or meeting_switched or too_long or session.recorder.poll() is not None:
                recorder = session.recorder
                finished_audio = session.audio_path
                finished_subject = session.detection.subject if session.detection else ""
                dashboard_recording = isinstance(recorder, DashboardCommandProcess)
                if not dashboard_recording and recorder.poll() is not None:
                    log_recorder_failure(config, recorder, finished_audio)
                if lost:
                    log(config, f"Meeting not seen for {misses * poll_seconds}s; stopping")
                stop_recording(config, recorder, stop_grace)
                session.clear()
                was_sticky = False
                # Forget the subject of the meeting that just ended so a lingering
                # meeting chat window cannot immediately restart a recording.
                if finished_subject and not meeting_switched and recent_subject.casefold() == finished_subject.casefold():
                    recent_subject = ""
                hits = 0
                misses = 0
                if finished_audio and not dashboard_recording:
                    transcribe(config, finished_audio)

        # Background work (one job at a time, never while recording): untranscribed recordings,
        # failed summaries and queued Huipputaso upgrades, retried with a growing delay (D3, D5).
        if catchup_proc is not None and catchup_proc.poll() is not None:
            ok = catchup_proc.returncode == 0
            log(config, f"Background {catchup_kind} {'done' if ok else f'failed (exit {catchup_proc.returncode})'}: {catchup_job}")
            if not ok and catchup_job is not None:
                notify_failure(config, catchup_kind, catchup_job)
            catchup_proc, catchup_job, catchup_kind = None, None, ""
        now = time.time()
        if (
            config.get("catchup_transcription", True)
            and catchup_proc is None
            and session.recorder is None
            and now - last_catchup_scan >= float(config.get("catchup_scan_minutes", 5)) * 60
            and not dashboard_is_recording(config, now)
        ):
            last_catchup_scan = now
            work = find_work(config, now)
            if work is not None:
                catchup_kind, pending = work
                log(config, f"Background {catchup_kind}: {pending.parent}")
                catchup_proc = start_catchup(config, pending, catchup_kind)
                catchup_job = pending.parent if catchup_proc else None

        # EU login check for the lamp (no API call, no cost).
        if config.get("agent_platform_project") and now - last_login_check >= 15 * 60:
            last_login_check = now
            ok, detail = policy.check_eu_login()
            policy.set_eu_login(ok, detail, config)
            if not ok:
                log(config, f"EU login not working: {detail}")

        time.sleep(poll_seconds)


def main() -> int:
    parser = argparse.ArgumentParser(description="Automatically record and transcribe Teams/Meet meetings.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--once", action="store_true", help="Run one detection pass and exit.")
    args = parser.parse_args()

    return watch(args.config, once=args.once)


if __name__ == "__main__":
    raise SystemExit(main())
