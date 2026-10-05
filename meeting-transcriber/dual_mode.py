#!/usr/bin/env python3
"""Huipputaso, global position (D9): two independent recognizers, an adjudicator listens.

Soniox (backbone: speaker turns with timestamps) and Microsoft MAI-Transcribe-2 (Azure Speech) each
transcribe the whole recording in one call. The recording is cut into ~10 minute windows at Soniox
turn boundaries; for every window gemini-3.8-flash on Agent Platform EU listens to the audio, sees
both transcripts and writes the final lines (max_mode.adjudicate).

Fallback chain, each step marked in the transcript and notified:
  Soniox + MAI -> Soniox + Gemini 3.5 Transcribe -> MAI + Gemini 3.5 Transcribe -> old Gemini Huipputaso.
"""

from __future__ import annotations

import base64
import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import max_mode as MM
import policy
import providers as P
import tarkka as T

WINDOW_SECONDS = 600
SONIOX_CACHE = "soniox.json"
MAI_CACHE = "mai.json"


def soniox_from_config(config: dict, vocab: list | None = None) -> P.SonioxTranscriber:
    return P.SonioxTranscriber(
        "soniox", base_url=str(config.get("soniox_base_url", "https://api.soniox.com")),
        model=str(config.get("soniox_model", "stt-async-v5")),
        language_hints=config.get("soniox_language_hints") or ["fi", "en"],
        ffmpeg=P.ffmpeg_path(config), custom_vocabulary=P.load_vocabulary(config) if vocab is None else vocab)


def mai_from_config(config: dict, vocab: list | None = None) -> P.AzureMaiTranscriber:
    return P.AzureMaiTranscriber(
        "mai", endpoints=config.get("azure_mai_endpoints") or [], locales=config.get("azure_mai_locales") or ["fi"],
        usage_file=config.get("azure_usage_file", "~/.meeting-transcriber/azure-kaytto.json"),
        paid_hours_per_month=float(config.get("azure_paid_hours_per_month", 50)),
        custom_vocabulary=P.load_vocabulary(config) if vocab is None else vocab)


def cached_run(job_dir: Path, name: str, engine, audio: Path, duration: float) -> dict | None:
    """Engine result for the whole recording, cached in the job folder (perustaso and retries reuse it)."""
    path = job_dir / name
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if data.get("turns"):
            return data
    except (OSError, ValueError):
        pass
    if not engine.available():
        return None
    try:
        data = (engine.transcribe_file(audio, duration) if isinstance(engine, P.AzureMaiTranscriber)
                else engine.transcribe_file(audio))
    except Exception as exc:  # noqa: BLE001
        T.log(f"{name}: {exc}")
        return None
    if not data.get("turns"):
        return None
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return data


def windows(turns: list, duration: float, size: float = WINDOW_SECONDS) -> list:
    """[(start, end)] covering the recording, cut at turn starts so no turn is split."""
    cuts, start = [0.0], 0.0
    for t in turns:
        if t["start"] - start >= size:
            cuts.append(t["start"])
            start = t["start"]
    edges = cuts + [max(duration, turns[-1]["end"] if turns else duration) + 1]
    return [(edges[i], edges[i + 1]) for i in range(len(edges) - 1)]


def in_window(turns: list, w: tuple) -> list:
    return [dict(t, start=t["start"] - w[0]) for t in turns if w[0] <= t["start"] < w[1]]


def run(config: dict, recording: Path, job_dir: Path, progress) -> str:
    """Write the Huipputaso outputs into job_dir and return the readable text. Raises when no
    two-ear route is available; the caller then runs the old Gemini Huipputaso."""
    import transcribe_recording as TR

    gen = P.text_generator(config)
    T._TEXT = gen
    project, location, _ = P.ap_settings(config)
    judge = (project, location, str(config.get("max_adjudicator_model", "gemini-3.8-flash")))
    judge_gen = P.judge_generator(config)  # global position: AI Studio if the EU login has expired
    vocab = P.load_vocabulary(config)
    ffmpeg = P.ffmpeg_path(config)
    duration = TR.recording_duration_seconds(recording) or 0.0
    audio = P.recording_mp3(ffmpeg, recording, job_dir / "audio-16k.mp3")

    TR.write_progress(progress, stage="transcribing", progress=0.1, message="Huipputaso: Soniox + MAI")
    with ThreadPoolExecutor(max_workers=2) as pool:
        fs = pool.submit(cached_run, job_dir, SONIOX_CACHE, soniox_from_config(config, vocab), audio, duration)
        fm = pool.submit(cached_run, job_dir, MAI_CACHE, mai_from_config(config, vocab), audio, duration)
        son, mai = fs.result(), fm.result()

    if son and mai:
        backbone, other, route = son, mai, "Soniox + Microsoft MAI"
    elif son or mai:
        backbone = son or mai
        other, route = None, ("Soniox + Gemini 3.5 Transcribe" if son else "Microsoft MAI + Gemini 3.5 Transcribe")
    else:
        raise P.ProviderError("Soniox ja MAI eivät vastanneet")
    degraded = route != "Soniox + Microsoft MAI"

    wins = windows(backbone["turns"], duration)
    wdir = job_dir / "ikkunat"
    wdir.mkdir(exist_ok=True)
    gemini = P.GeminiTranscriber("gemini", mode="verbatim", custom_vocabulary=vocab, ffmpeg=ffmpeg,
                                 language_codes=list(config.get("gemini_language_codes") or ["fi-FI"]))

    def window_audio(i: int, w: tuple) -> Path:
        out = wdir / f"ikkuna-{i:02d}.mp3"
        if not out.exists():
            subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{w[0]:.2f}",
                            "-to", f"{min(w[1], duration or w[1]):.2f}", "-i", str(audio), "-c", "copy", str(out)],
                           check=True)
        return out

    def second_ear(i: int, w: tuple, clip: Path) -> str:
        if other:
            return " ".join(t["text"] for t in in_window(other["turns"], w))
        cache = wdir / f"gemini-{i:02d}.txt"
        if cache.exists():
            return cache.read_text(encoding="utf-8")
        if not gemini.available():
            return ""
        try:
            text, _ = gemini.transcribe(clip)
        except Exception as exc:  # noqa: BLE001
            T.log(f"window {i}: Gemini second ear failed: {exc}")
            return ""
        cache.write_text(text, encoding="utf-8")
        return text

    def judge_one(i: int) -> dict:
        w = wins[i]
        b1 = in_window(backbone["turns"], w)
        if not b1:
            return {"lines": [], "method": "empty window"}
        path = wdir / f"tuomio-{i:02d}.json"
        source = f"d1:{route}:{judge[2]}:{len(b1)}"
        try:
            prev = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            if prev.get("source") == source and prev.get("lines"):
                return prev
        except (OSError, ValueError):
            pass
        clip = window_audio(i, w)
        a1 = second_ear(i, w, clip)
        part = {"inlineData": {"mimeType": "audio/mpeg", "data": base64.b64encode(clip.read_bytes()).decode("ascii")}}
        result = MM.adjudicate(judge, i, part, {"b": {"turns": b1}, "a": {"text": a1}}, {}, vocab, max_ratio=1.6,
                               generate=judge_gen)
        result.pop("backbone", None)
        result["source"] = source
        path.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
        return result

    TR.write_progress(progress, stage="transcribing", progress=0.45, message=f"Huipputaso: tuomari kuuntelee ({len(wins)} jaksoa)")
    with ThreadPoolExecutor(max_workers=int(config.get("max_parallel_adjudications", 3))) as pool:
        judged = list(pool.map(judge_one, range(len(wins))))

    segments = []
    for (w0, _), j in zip(wins, judged):
        lines = [dict(l, time=T.hms(w0 + T.mmss(l["time"]))) for l in j["lines"]]
        segments.append({"lines": lines, "method": j["method"]})

    TR.write_progress(progress, stage="transcribing", progress=0.7, message="Huipputaso: puhujat")
    # Soniox speaker labels hold for the whole recording, so name them once for the whole meeting.
    whole = [{"lines": [l for s in segments for l in s["lines"]]}]
    mapping = MM.unify_speakers(gen, whole, str(config.get("meeting_owner", "") or ""))
    for seg in segments:
        seg["lines"] = [dict(l, speaker=mapping.get(f"1:{l['speaker']}", f"Puhuja {l['speaker'].lstrip('S')}"))
                        for l in seg["lines"]]

    TR.write_progress(progress, stage="transcribing", progress=0.78, message="Huipputaso: luettava versio")
    with ThreadPoolExecutor(max_workers=3) as pool:
        cleaned = list(pool.map(lambda s: T.clean_segment(None, "", s["lines"]), segments))
    for i, c in enumerate(cleaned):
        if len(c["lines"]) == len(segments[i]["lines"]):
            cleaned[i] = dict(c, lines=[dict(l, time=v["time"], speaker=v["speaker"])
                                        for l, v in zip(c["lines"], segments[i]["lines"])])

    verbatim = [l for s in segments for l in s["lines"]]
    readable = [l for c in cleaned for l in c["lines"]]
    names = sorted({v for v in mapping.values() if not v.startswith("Puhuja")})
    uncertain = sum(l["text"].count("[?]") for l in verbatim)
    header = [f"Huipputaso: {route}; tuomarimalli {judge[2]} kuunteli äänen ja ratkaisi erot.",
              "Puhujat nimetty tekstin perusteella, tarkista: " + (", ".join(names) or "ei varmoja nimiä") + ".",
              f"[?] = epävarma kohta, tarkista kuuntelemalla ({uncertain} kpl)."]
    failed = [f"jakso {i + 1}" for i, s in enumerate(segments) if s["method"].startswith("backbone only")]
    if degraded:
        header.insert(0, f"⚠️ Heikennetty Huipputaso: {'MAI' if son else 'Soniox'} ei vastannut, käytettiin {route}.")
    if failed:
        header.append("Huom: tuomari ei saanut ratkaistua, runko sellaisenaan: " + ", ".join(failed) + ".")
    head = "\n".join(f"> {h}" for h in header)
    (job_dir / "transcript.md").write_text(
        f"# Litteraatti (huipputaso, sanatarkka)\n\n{head}\n\n{T.format_lines(verbatim)}\n", encoding="utf-8")
    (job_dir / "transcript-luettava.md").write_text(
        f"# Litteraatti (huipputaso, luettava)\n\n{head}\n\n{T.format_lines(readable)}\n", encoding="utf-8")
    (job_dir / "transcript.txt").write_text(T.format_lines(verbatim) + "\n", encoding="utf-8")
    (job_dir / "huipputaso.json").write_text(json.dumps({
        "reitti": route, "heikennetty": degraded, "tuomari": judge[2],
        "mai_resurssi": (mai or {}).get("endpoint"), "jaksot": [{"alku": w[0], "loppu": w[1], "menetelma": s["method"]}
                                                                for w, s in zip(wins, segments)],
        "puhujat": mapping, "epavarmat": uncertain}, ensure_ascii=False, indent=1), encoding="utf-8")
    if degraded:
        policy.notify("Huipputaso heikennetty", f"{job_dir.name}: {route} (toinen palvelu ei vastannut).")
    return T.format_lines(readable)
