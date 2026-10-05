#!/usr/bin/env python3
"""Huipputaso ("max") transcription preset.

Per ~10 min snippet (cut at pauses), four independent machine transcripts of the same audio:
  T-A  Gemini 3.5 Transcribe, verbatim + custom vocabulary         (AI Studio)
  T-B  Gemini 3.5 Transcribe, verbatim + diarization + word times  (AI Studio)  <- backbone
  F-A  Gemini Flash, verbatim prompt + vocabulary                  (Agent Platform EU)
  F-B  Gemini Flash, speaker turns as JSON                         (Agent Platform EU)
An adjudicator model (default gemini-3.8-flash, EU) LISTENS to the snippet audio together with the
four transcripts and writes the final verbatim line for every backbone turn, marking unresolved
words with [?]. Snippets are adjudicated in parallel; a final text call unifies speaker labels across
snippets and names speakers where the transcript makes it clear. Then the readable version.
"""
from __future__ import annotations

import base64
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import providers as P
import tarkka as T


def adjudicate_prompt(b1: list, a1: str, a2: str, b2: list, vocab: list) -> str:
    def fmt(turns):
        return "\n".join(f"[{T.hms(t['start'])}] {t['speaker']}: {t['text']}" for t in turns)
    return f"""You produce the final, most accurate verbatim transcript of the attached Finnish meeting audio
segment. Four machine transcripts of the SAME audio are given below. Times are from the start of this segment.

B1 (speaker turns with reliable timestamps; this is the backbone):
{fmt(b1)}

A1 (plain text, recognizer with custom vocabulary):
{a1 or "-"}

A2 (plain text, a second, independent model):
{a2 or "-"}

B2 (speaker turns from the second model; timestamps approximate):
{fmt(b2) or "-"}

Known names and terms (spell exactly like this): {"; ".join(vocab[:300]) or "-"}

Output exactly one line per B1 line, in the same order and with the same timestamp, in the form
[hh:mm:ss] LABEL: text
Rules:
- LABEL is the B1 speaker label (e.g. S0). Change it only if the audio clearly shows another B1 speaker.
- Wherever the transcripts disagree, LISTEN to the audio and write what is actually said. When all agree,
  keep that wording.
- Strictly verbatim: keep spoken forms (mä, sä, et, niinku), filler words, repetitions and false starts.
  English words and expressions stay in English as spoken. Never translate, summarize, add or drop content.
- If you still cannot tell what was said, write the most likely reading and append [?] to that word.
- Output only the lines, nothing else."""


def unify_prompt(samples: str, owner: str) -> str:
    owner_hint = f"The recording was made by {owner}, who is very likely one of the speakers. " if owner else ""
    return f"""A meeting was transcribed in segments; speaker labels are local to each segment (segment:label).
{owner_hint}Below are sample lines per local label and the lines at each segment boundary.
Map every local label to one global speaker. Use the person's real name only if the transcript makes it
clear (self-introduction, addressed by name and answering); otherwise use "Puhuja 1", "Puhuja 2", ... and
keep the same global label for the same person across segments. Reply with JSON only, for example
{{"1:S0": "Etunimi Sukunimi", "1:S1": "Puhuja 2", "2:S0": "Etunimi Sukunimi"}}.

{samples}"""


def label(speaker) -> str:
    """Colon-free speaker label (S0, S1, ...): the transcript line format uses ':' as the separator."""
    s = str(speaker or "")
    m = re.search(r"(\d+)\s*$", s)
    return f"S{m.group(1)}" if m else (re.sub(r"[^\w]+", "", s) or "S?")


def turns_of(b: dict, offset: float = 0.0) -> list:
    if b.get("turns"):
        turns = [{"speaker": t["speaker"], "start": offset + t["start"], "text": t["text"]} for t in b["turns"]]
    else:
        turns = T.build_turns(b.get("text", ""), b.get("words", []), offset)
    return [dict(t, speaker=label(t["speaker"])) for t in turns]


def adjudicate(ap: tuple, idx: int, audio_part: dict, ts: dict, fl: dict, vocab: list,
               max_ratio: float = 1.3) -> dict:
    """Final lines for one snippet, times relative to the snippet start (fixed later)."""
    b1 = turns_of(ts.get("b", {})) or turns_of(fl.get("b", {}))
    if not b1:
        text = ts.get("a", {}).get("text") or fl.get("a", {}).get("text") or ""
        return {"lines": [{"time": "00:00:00", "speaker": "S?", "text": text}] if text else [],
                "method": "no speaker data", "backbone": []}
    prompt = adjudicate_prompt(b1, ts.get("a", {}).get("text", ""), fl.get("a", {}).get("text", ""),
                               turns_of(fl.get("b", {})), vocab)
    project, location, model = ap
    out = ""
    for attempt, wait in enumerate(T.RETRY_WAITS):
        time.sleep(wait)
        try:
            out = P.ap_generate(project, location, model, [{"text": prompt}, audio_part], temperature=0.0,
                                max_output_tokens=65536, thinking_budget=4096)
        except Exception as exc:  # noqa: BLE001
            T.log(f"adjudicate {idx} attempt {attempt + 1} failed: {exc}")
            continue
        lines = T.parse_lines(out)
        bw = sum(T.word_count(t["text"]) for t in b1)
        lw = sum(T.word_count(l["text"]) for l in lines)
        # A cleaned backbone (Soniox) is shorter than the verbatim result, hence max_ratio.
        if len(lines) >= 0.8 * len(b1) and 0.75 * bw <= lw <= max_ratio * bw:
            return {"lines": lines, "method": "adjudicated (4 sources + audio)", "backbone": b1}
        T.log(f"adjudicate {idx} attempt {attempt + 1} rejected: {len(lines)}/{len(b1)} lines, {lw}/{bw} words")
    lines = [{"time": T.hms(t["start"]), "speaker": t["speaker"], "text": t["text"]} for t in b1]
    return {"lines": lines, "method": "backbone only (adjudication failed)", "backbone": b1}


NAME_CUE = re.compile(r"\b(nimi on|mä oon|mä olen|minä olen|olen|oon|mun nimi|my name is|I'm|I am)\s+(tosiaan\s+)?"
                      r"[A-ZÅÄÖ][a-zåäö]+\s+[A-ZÅÄÖ][a-zåäö]+", re.U)


def unify_speakers(gen, segments: list, owner: str) -> dict:
    parts = []
    for i, seg in enumerate(segments, start=1):
        by_label: dict = {}
        for l in seg["lines"]:
            by_label.setdefault(l["speaker"], []).append(l["text"])
        for label, texts in by_label.items():
            longest = sorted(texts, key=len, reverse=True)[:2]
            parts.append(f"{i}:{label} samples: " + " | ".join(t[:300] for t in longest))
            # Self-introductions and direct address are the evidence for real names; they are often
            # deep inside a long turn, so quote every sentence that looks like one.
            intro = [s.strip() for t in texts for s in re.split(r"(?<=[.!?])\s+", t)
                     if NAME_CUE.search(s)][:4]
            if intro:
                parts.append(f"{i}:{label} name evidence: " + " | ".join(s[:200] for s in intro))
        if seg["lines"]:
            first, last = seg["lines"][0], seg["lines"][-1]
            parts.append(f"segment {i} starts: {i}:{first['speaker']}: {first['text'][:160]}")
            parts.append(f"segment {i} ends: {i}:{last['speaker']}: {last['text'][:160]}")
    try:
        out = gen(unify_prompt("\n".join(parts), owner), temperature=0.0)
        m = re.search(r"\{.*\}", out or "", re.S)
        data = json.loads(m.group(0)) if m else {}
    except Exception as exc:  # noqa: BLE001
        T.log(f"speaker unification failed: {exc}")
        data = {}
    return {str(k): str(v).strip() for k, v in data.items() if str(v).strip() and len(str(v)) <= 60}


def run(config: dict, recording: Path, job_dir: Path, progress, use_transcribe: bool = True) -> str:
    """Run the max pipeline, write outputs into job_dir, return the readable transcript text.

    use_transcribe=False is the EU version: gemini-3.5-transcribe (AI Studio only) is skipped and the
    two Flash passes on Agent Platform EU are the only sources for the adjudicator.
    Snippets are shared with perustaso (job_dir/snippets), whose verbatim pass is reused as T-A
    (global) or F-A (EU), and snippets perustaso found silent are skipped."""
    import transcribe_recording as TR

    gen = P.text_generator(config)
    T._TEXT = gen  # tarkka.clean_segment uses the module-level generator
    key = P.gemini_key(None)
    project, location, _ = P.ap_settings(config)
    ap_audio = (project, location, str(config.get("agent_platform_audio_model", "gemini-3.5-flash")))
    ap_judge = (project, location, str(config.get("max_adjudicator_model", "gemini-3.8-flash")))
    langs = list(config.get("gemini_language_codes") or ["fi-FI"])
    vocab = P.load_vocabulary(config)
    owner = str(config.get("meeting_owner", "") or "")
    ffmpeg = P.ffmpeg_path(config)
    sdir = job_dir / "snippets"  # shared with perustaso
    snippets = TR.split_snippets(
        recording, sdir, int(config.get("snippet_seconds", 600)), progress,
        max_seconds=int(config.get("snippet_max_seconds", 780)), split_mode="silence",
        window_seconds=int(config.get("snippet_silence_window_seconds", 120)),
        silence_db=float(config.get("snippet_silence_db", -35.0)),
    )
    offsets = T.snippet_offsets(snippets, sdir / "cuts.json", TR.recording_duration_seconds)
    cache = sdir / "transcripts"
    cache.mkdir(parents=True, exist_ok=True)

    def load(path: Path) -> dict:
        try:
            return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except (OSError, ValueError):
            return {}

    def audio_part(snippet: Path) -> dict:
        mp3 = T.to_mp3(ffmpeg, snippet)
        return {"inlineData": {"mimeType": "audio/mpeg", "data": base64.b64encode(mp3.read_bytes()).decode("ascii")}}

    silence_db = float(config.get("silence_mean_db", TR.DEFAULT_SILENCE_MEAN_DB))

    def perustaso(snippet: Path) -> dict:
        """The perustaso result for this snippet: {"text", "raw"} or {}."""
        data = load(cache / f"{snippet.stem}.json")
        return data if isinstance(data.get("raw"), dict) else {}

    def silent(snippet: Path) -> bool:
        raw = perustaso(snippet).get("raw", {})
        if raw.get("skipped") == "silence":
            return True
        if raw:
            return False
        return TR.snippet_is_silent(TR.snippet_mean_volume(snippet), silence_db)

    def ts_one(snippet: Path) -> dict:  # Gemini 3.5 Transcribe, two passes (AI Studio)
        empty = {"a": {"text": ""}, "b": {"text": "", "words": []}}
        if not use_transcribe or silent(snippet):
            return empty
        path = cache / f"{snippet.stem}.ts.json"
        data = load(path)
        if data.get("a", {}).get("text") and data.get("b", {}).get("words"):
            return data
        if not key:
            return empty
        base = perustaso(snippet)
        raw = base.get("raw", {})
        if (not data.get("a", {}).get("text") and base.get("text") and raw.get("model") == P.GEMINI_TRANSCRIBE_MODEL
                and str(raw.get("mode")) == "verbatim" and raw.get("vocabulary")):
            data["a"] = {"text": base["text"], "model": P.GEMINI_TRANSCRIBE_MODEL, "reused": "perustaso"}
        uri = P.gemini_upload_file(key, T.to_flac(ffmpeg, snippet), "audio/flac")
        if not data.get("a", {}).get("text"):
            data["a"] = T.pass_a(key, uri, langs, vocab, "")
        if not data.get("b", {}).get("words"):
            data["b"] = T.pass_b(key, uri, langs)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        return data

    def fl_one(snippet: Path) -> dict:  # Gemini Flash, two passes (Agent Platform EU)
        if silent(snippet):
            return {"a": {"text": ""}, "b": {"text": "", "turns": []}, "silent": True}
        path = cache / f"{snippet.stem}.fl.json"
        data = load(path)
        if data.get("a", {}).get("text") and data.get("b", {}).get("turns"):
            return data
        base = perustaso(snippet)
        raw = base.get("raw", {})
        if (not data.get("a", {}).get("text") and base.get("text") and raw.get("prompt") == P.FLASH_PLAIN_PROMPT_VERSION
                and raw.get("model") == f"agent_platform:{ap_audio[1]}:{ap_audio[2]}" and not raw.get("eu_fallback")):
            data["a"] = {"text": base["text"], "model": f"{ap_audio[1]}:{ap_audio[2]}", "reused": "perustaso"}
        part = audio_part(snippet)
        if not data.get("a", {}).get("text"):
            data["a"] = T.pass_a_flash(ap_audio, part, vocab)
        if not data.get("b", {}).get("turns"):
            data["b"] = T.pass_b_flash(ap_audio, part, vocab)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        return data

    TR.write_progress(progress, stage="transcribing", progress=0.1,
                      message=f"Huipputaso: {len(snippets)} snippets")
    with ThreadPoolExecutor(max_workers=2) as ts_pool, ThreadPoolExecutor(max_workers=3) as fl_pool:
        ts_f = [ts_pool.submit(ts_one, s) for s in snippets]
        fl_f = [fl_pool.submit(fl_one, s) for s in snippets]
        ts_all = [f.result() for f in ts_f]
        fl_all = [f.result() for f in fl_f]

    TR.write_progress(progress, stage="transcribing", progress=0.55, message="Max: adjudicating with audio")

    def judge_one(i: int) -> dict:
        path = cache / f"{snippets[i].stem}.judged.json"
        source = f"v2:{ap_judge[2]}:{len(ts_all[i].get('b', {}).get('words', []))}:" \
                 f"{len(fl_all[i].get('b', {}).get('turns', []))}"
        prev = load(path)
        if prev.get("source") == source and prev.get("lines"):
            return prev
        if fl_all[i].get("silent"):
            return {"lines": [], "method": "adjudicated (silent snippet skipped)", "backbone": []}
        result = adjudicate(ap_judge, i, audio_part(snippets[i]), ts_all[i], fl_all[i], vocab)
        result["source"] = source
        path.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
        return result

    with ThreadPoolExecutor(max_workers=int(config.get("max_parallel_adjudications", 3))) as pool:
        judged = list(pool.map(judge_one, range(len(snippets))))

    segments = []
    for i, j in enumerate(judged):
        end = offsets[i + 1] if i + 1 < len(offsets) else None
        backbone = ts_all[i].get("b", {}) if ts_all[i].get("b", {}).get("words") else fl_all[i].get("b", {})
        lines = [dict(l, time=T.hms(offsets[i] + T.mmss(l["time"]))) for l in j["lines"]]
        segments.append({"lines": T.fix_times(lines, backbone, offsets[i], end), "method": j["method"]})

    TR.write_progress(progress, stage="transcribing", progress=0.7, message="Max: unifying speakers")
    mapping = unify_speakers(gen, segments, owner)
    for i, seg in enumerate(segments, start=1):
        seg["lines"] = [dict(l, speaker=mapping.get(f"{i}:{l['speaker']}", f"Puhuja {i}.{l['speaker']}"))
                        for l in seg["lines"]]

    TR.write_progress(progress, stage="transcribing", progress=0.78, message="Max: readable version")
    with ThreadPoolExecutor(max_workers=3) as pool:
        cleaned = list(pool.map(lambda s: T.clean_segment(key, "", s["lines"]), segments))
    for i, c in enumerate(cleaned):
        if len(c["lines"]) == len(segments[i]["lines"]):
            cleaned[i] = dict(c, lines=[dict(l, time=v["time"], speaker=v["speaker"])
                                        for l, v in zip(c["lines"], segments[i]["lines"])])

    verbatim_lines = [l for s in segments for l in s["lines"]]
    readable_lines = [l for c in cleaned for l in c["lines"]]
    names = sorted({v for v in mapping.values() if not v.startswith("Puhuja")})
    methods = [s["method"] for s in segments]
    uncertain = sum(l["text"].count("[?]") for l in verbatim_lines)
    sources = ("neljä litterointia (Gemini 3.5 Transcribe ×2, Gemini Flash EU ×2)" if use_transcribe
               else "kaksi litterointia (Gemini Flash EU ×2)")
    header = [
        f"Huipputaso: {sources}; tuomarimalli {ap_judge[2]} (EU) kuunteli äänen ja ratkaisi erot.",
        "Puhujat yhdistetty ja nimetty tekstin perusteella, tarkista: " + (", ".join(names) or "ei varmoja nimiä") + ".",
        f"[?] = epävarma kohta, tarkista kuuntelemalla ({uncertain} kpl).",
    ]
    fallbacks = [f"jakso {i + 1}: {m}" for i, m in enumerate(methods) if not m.startswith("adjudicated")]
    if fallbacks:
        header.append("Huom: " + "; ".join(fallbacks) + ".")
    head = "\n".join(f"> {h}" for h in header)
    (job_dir / "transcript.md").write_text(
        f"# Litteraatti (huipputaso, sanatarkka)\n\n{head}\n\n{T.format_lines(verbatim_lines)}\n", encoding="utf-8")
    (job_dir / "transcript-luettava.md").write_text(
        f"# Litteraatti (huipputaso, luettava)\n\n{head}\n\n{T.format_lines(readable_lines)}\n", encoding="utf-8")
    (job_dir / "transcript.txt").write_text(T.format_lines(verbatim_lines) + "\n", encoding="utf-8")
    (job_dir / "max.json").write_text(json.dumps({
        "adjudicator": ap_judge[2], "audio_flash": ap_audio[2], "text_model": getattr(gen, "label", "?"),
        "snippets": [{"file": str(s), "offset": o, "method": m} for s, o, m in zip(snippets, offsets, methods)],
        "speaker_mapping": mapping, "uncertain_marks": uncertain,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    return T.format_lines(readable_lines)
