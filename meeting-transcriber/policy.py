#!/usr/bin/env python3
"""Käsittelysijainti- ja laatukytkimet, niiden lukitus palaveriin, kutsuloki ja punainen lamppu.

Päätökset: docs/PAATOKSET.md (D1-D5).

- Kytkimet ovat tiedostossa (oletus ~/.meeting-transcriber/kytkimet.json), jota Dashboard muokkaa:
    {"sijainti": "maailmanlaajuinen" | "eu", "laatu": "perus" | "huippu"}
- Asento lukitaan palaverin työkansioon (job.json) nauhoituksen alkaessa ja uudelleen litteroinnin
  alkaessa. Tiukempi voittaa: EU, jos EU oli päällä kummalla tahansa hetkellä; Huipputaso samoin.
- Jokainen ulkoinen mallikutsu kirjataan työkansioon (kutsut.jsonl): osoite, polun tyyppi, malli,
  tulos. API-avainta ei koskaan kirjata.
- EU-asennossa saa vaihtaa EU:n ulkopuolelle, jos EU-palvelu ei vastaa, mutta silloin syttyy
  punainen lamppu (lamppu.json, Dashboard) ja litteraatin alkuun tulee merkintä.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
import threading
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

GLOBAL = "maailmanlaajuinen"
EU = "eu"
PERUS = "perus"
HUIPPU = "huippu"
SIJAINNIT = (GLOBAL, EU)
LAADUT = (PERUS, HUIPPU)
DEFAULTS = {"sijainti": GLOBAL, "laatu": PERUS}

EU_HOSTS = frozenset({"aiplatform.eu.rep.googleapis.com"})
RUNTIME = Path("~/.meeting-transcriber").expanduser()
JOB_FILE = "job.json"
CALL_LOG = "kutsut.jsonl"


def now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def switch_file(config: dict[str, Any] | None = None) -> Path:
    return Path(str((config or {}).get("switch_file") or RUNTIME / "kytkimet.json")).expanduser()


def lamp_file(config: dict[str, Any] | None = None) -> Path:
    return Path(str((config or {}).get("lamp_file") or RUNTIME / "lamppu.json")).expanduser()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def normalize(switches: dict[str, Any] | None) -> dict[str, str]:
    """Validated switch values; anything unknown falls back to the default position."""
    s = dict(DEFAULTS)
    for key, allowed in (("sijainti", SIJAINNIT), ("laatu", LAADUT)):
        value = str((switches or {}).get(key, "")).strip().lower()
        if value in allowed:
            s[key] = value
    return s


def read_switches(config: dict[str, Any] | None = None) -> dict[str, str]:
    return normalize(_read_json(switch_file(config)))


def write_switches(switches: dict[str, Any], config: dict[str, Any] | None = None) -> dict[str, str]:
    s = normalize(switches)
    _write_json(switch_file(config), s)
    return s


def stricter(a: dict[str, str], b: dict[str, str]) -> dict[str, str]:
    """EU beats global and Huipputaso beats perustaso: a later switch change can only tighten a job."""
    return {
        "sijainti": EU if EU in (a.get("sijainti"), b.get("sijainti")) else GLOBAL,
        "laatu": HUIPPU if HUIPPU in (a.get("laatu"), b.get("laatu")) else PERUS,
    }


def lock_job(job_dir: Path, switches: dict[str, Any], moment: str) -> dict[str, Any]:
    """Record the switch positions at `moment` and return the job's effective (locked) settings."""
    path = job_dir / JOB_FILE
    job = _read_json(path)
    now = normalize(switches)
    effective = stricter(normalize(job), now) if job else now
    history = list(job.get("historia") or [])
    history.append({"hetki": moment, "aika": now_iso(), **now})
    job.update(effective)
    job["historia"] = history
    job.setdefault("luotu", now_iso())
    _write_json(path, job)
    return job


def job_settings(job_dir: Path) -> dict[str, str]:
    return normalize(_read_json(job_dir / JOB_FILE))


# --------------------------------------------------------------------------- #
# Kutsuloki
# --------------------------------------------------------------------------- #

_LOG: dict[str, Any] = {"path": None}
_LOCK = threading.Lock()
_MODEL_RE = re.compile(r"/models/([^/:?]+)")


def set_call_log(job_dir: Path | None) -> None:
    _LOG["path"] = (job_dir / CALL_LOG) if job_dir else None


def describe_request(url: str, body: bytes | None = None) -> dict[str, str]:
    """Host, kind and model of a request, without the query string (the API key travels there)."""
    parts = urlsplit(url)
    path = parts.path
    if "/upload/" in path:
        kind = "upload"
    elif path.endswith("/interactions"):
        kind = "interactions"
    elif ":generateContent" in path:
        kind = "generateContent"
    else:
        kind = "other"
    model = ""
    match = _MODEL_RE.search(path)
    if match:
        model = match.group(1)
    elif kind == "interactions" and body:
        try:
            model = str(json.loads(body.decode("utf-8")).get("model", ""))
        except (ValueError, UnicodeDecodeError):
            model = ""
    return {"host": parts.hostname or "", "kind": kind, "model": model}


def record_call(url: str, body: bytes | None, outcome: str) -> None:
    path = _LOG["path"]
    if path is None:
        return
    entry = {"aika": now_iso(), **describe_request(url, body), "tulos": outcome}
    line = json.dumps(entry, ensure_ascii=False)
    with _LOCK:
        try:
            with Path(path).open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError:
            pass


def calls(job_dir: Path) -> list[dict[str, Any]]:
    path = job_dir / CALL_LOG
    out = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    except OSError:
        pass
    return out


def outside_eu(job_dir: Path) -> list[str]:
    """Hosts outside the EU endpoint that this job called (any attempt counts, data was sent)."""
    return sorted({c.get("host", "") for c in calls(job_dir) if c.get("host") and c.get("host") not in EU_HOSTS})


def status_line(job_dir: Path) -> str:
    s = job_settings(job_dir)
    laatu = "Huipputaso" if s["laatu"] == HUIPPU else "Perustaso"
    if s["sijainti"] == EU:
        hosts = outside_eu(job_dir)
        if hosts:
            return (f"> 🔴 **Käsitelty: EU-asento, mutta osa käsittelystä tehtiin EU:n ulkopuolella** "
                    f"({', '.join(hosts)}), koska EU-palvelu ei vastannut. {laatu}.")
        return f"> 🇪🇺 **Käsitelty: EU** (vain EU-palvelu). {laatu}."
    return f"> 🌍 **Käsitelty: maailmanlaajuinen** (Google AI Studio sallittu). {laatu}."


def stamp_outputs(job_dir: Path, names: tuple[str, ...] = ("transcript.md", "transcript-luettava.md", "summary.md")) -> None:
    """Put the processing-location line right under the first heading of each output (idempotent)."""
    line = status_line(job_dir)
    for name in names:
        path = job_dir / name
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        lines = [l for l in text.splitlines() if not l.startswith("> 🔴 **Käsitelty")
                 and not l.startswith("> 🇪🇺 **Käsitelty") and not l.startswith("> 🌍 **Käsitelty")]
        if lines and lines[0].startswith("#"):
            new = [lines[0], "", line] + lines[1:]
        else:
            new = [line, ""] + lines
        path.write_text("\n".join(new).rstrip() + "\n", encoding="utf-8")


# --------------------------------------------------------------------------- #
# Punainen lamppu
# --------------------------------------------------------------------------- #

def read_lamp(config: dict[str, Any] | None = None) -> dict[str, Any]:
    lamp = _read_json(lamp_file(config))
    lamp.setdefault("rikkeet", [])
    return lamp


def report_violation(job_dir: Path, config: dict[str, Any] | None = None) -> bool:
    """If an EU job called a non-EU host, add it to the lamp and notify. Returns True if it did."""
    if job_settings(job_dir)["sijainti"] != EU:
        return False
    hosts = outside_eu(job_dir)
    if not hosts:
        return False
    lamp = read_lamp(config)
    if not any(r.get("palaveri") == job_dir.name for r in lamp["rikkeet"]):
        lamp["rikkeet"].append({"palaveri": job_dir.name, "osoitteet": hosts, "aika": now_iso()})
        _write_json(lamp_file(config), lamp)
        notify("EU:n ulkopuolella käsitelty",
               f"Palaveri {job_dir.name} käsiteltiin osin EU:n ulkopuolella, koska EU-palvelu ei vastannut.")
    return True


def set_eu_login(ok: bool, detail: str, config: dict[str, Any] | None = None) -> None:
    lamp = read_lamp(config)
    lamp["eu_kirjautuminen"] = {"ok": bool(ok), "tarkistettu": now_iso(), "tieto": detail[:200]}
    _write_json(lamp_file(config), lamp)


def check_eu_login(gcloud: str | None = None) -> tuple[bool, str]:
    """Can we get an Agent Platform token? (gcloud ADC; no API call, no cost)."""
    import shutil

    binary = gcloud or shutil.which("gcloud") or "/opt/homebrew/bin/gcloud"
    try:
        result = subprocess.run([binary, "auth", "application-default", "print-access-token"],
                                capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"gcloud ei toimi: {exc}"
    if result.returncode != 0 or not result.stdout.strip():
        return False, (result.stderr.strip().splitlines() or ["kirjautuminen puuttuu"])[-1]
    return True, "ok"


def notify(title: str, message: str) -> None:
    """macOS notification; never fails the caller."""
    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace('"', '\\"')

    try:
        subprocess.run(["osascript", "-e",
                        f'display notification "{esc(message)}" with title "Meeting Transcriber" '
                        f'subtitle "{esc(title)}"'],
                       capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        pass
