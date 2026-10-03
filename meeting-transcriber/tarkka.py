#!/usr/bin/env python3
"""Tarkka (accurate) transcription preset for Gemini 3.5 Transcribe.

Per snippet (cut at pauses, <= 29 min so diarization is allowed):
  A) verbatim pass WITH custom vocabulary  -> best wording (names, terms)
  B) verbatim pass WITH diarization + word timestamps -> speakers and times
Then, per snippet, a text-only Gemini Flash call merges B's speaker turns with
A's wording, a second call makes a readable version (fillers removed), and one
final call infers real speaker names only where the transcript makes them clear.

Every model output is validated; on failure the step falls back to the safer
source (B turns, or A text) instead of passing questionable text through.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
import zlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import providers as P

_TEXT = None  # text generator callable, set in run() from config (EU Agent Platform or Gemini API)
LINE_RE = re.compile(r"^\[(\d{2}:\d{2}:\d{2})\]\s*([^:\]]{1,60}):\s*(.*)$")
DIARIZE_MODE = {"type": "verbatim", "diarization_mode": "speaker", "timestamp_granularities": ["word"]}


def hms(seconds: float) -> str:
    s = int(max(0.0, seconds))
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def log(msg: str) -> None:
    print(f"tarkka: {msg}", file=sys.stderr)


def build_turns(text: str, words: list, offset: float) -> list:
    """Group diarized words into speaker turns; slice the original text so punctuation survives."""
    turns: list = []
    for i, w in enumerate(words):
        if not turns or turns[-1]["speaker"] != w["speaker"]:
            turns.append({"speaker": w["speaker"], "start": offset + w["start"], "first": i, "last": i})
        else:
            turns[-1]["last"] = i
    # The API's start_index/end_index are UTF-8 byte offsets, not character offsets (ä/ö are 2 bytes).
    raw = text.encode("utf-8")
    out = []
    for n, t in enumerate(turns):
        first = words[t["first"]]
        nxt = words[turns[n + 1]["first"]] if n + 1 < len(turns) else None
        si = first.get("start_index")
        ei = nxt.get("start_index") if nxt else len(raw)
        if isinstance(si, int) and isinstance(ei, int) and 0 <= si < ei <= len(raw):
            chunk = raw[si:ei].decode("utf-8", "ignore").strip()
        else:
            chunk = " ".join(words[k]["text"] for k in range(t["first"], t["last"] + 1))
        if chunk:
            out.append({"speaker": t["speaker"], "start": t["start"], "text": chunk})
    return out


def parse_lines(text: str) -> list:
    lines = []
    for raw in (text or "").splitlines():
        m = LINE_RE.match(raw.strip())
        if m:
            lines.append({"time": m.group(1), "speaker": m.group(2).strip(), "text": m.group(3).strip()})
    return lines


def format_lines(lines: list) -> str:
    return "\n".join(f"[{l['time']}] {l['speaker']}: {l['text']}" for l in lines)


def word_count(text: str) -> int:
    return len(re.findall(r"\w+", text or ""))


def merge_prompt(b_turns: list, a_text: str, vocab: list, roster: str, tail: str) -> str:
    b_lines = "\n".join(f"[{hms(t['start'])}] {t['speaker']}: {t['text']}" for t in b_turns)
    return f"""You merge two machine transcripts of the same Finnish meeting audio segment.

SOURCE B (speaker turns with timestamps; speaker changes and timestamps are reliable, wording less so):
{b_lines}

SOURCE A (same audio as plain text; wording is more accurate, especially names and terms):
{a_text}

Known names and terms (spell exactly like this): {"; ".join(vocab[:300]) or "-"}

Speakers identified in earlier segments: {roster or "none (this is the first segment)"}
End of the previous segment:
{tail or "-"}

Output the transcript of THIS segment as lines exactly in the form
[hh:mm:ss] Puhuja N: text
Rules:
- One output line per SOURCE B line, same order, same timestamp.
- Map each spk label to a "Puhuja N" label, consistently within the segment and consistent with the earlier
  segments when the context makes it clear (the same person continues across the boundary, or is addressed or
  referred to). If unsure, use a new number rather than merging two different people.
- The text of each line is the words of that B turn, corrected with SOURCE A where A clearly has the better
  reading of the same words: names and terms from the list above, or a B word that is not a real word or makes
  no sense in context. Never replace a real, sensible word in B with an odd or non-existent word from A.
  Stay verbatim: keep filler words and repetitions as in B. Never add, summarize, reorder or drop content.
- If A and B disagree and you cannot tell which is right, keep B's word and append [?] to it.
- Output only the lines, nothing else."""


def clean_prompt(verbatim: str) -> str:
    return f"""Below is a verbatim Finnish meeting transcript, one line per speaker turn, in the form
[hh:mm:ss] Speaker: text

Make it readable: remove filler words (e.g. niinku, tota, tota noin, niin kuin used as filler, öö, ää),
stutters, immediately repeated words and false starts, and fix punctuation and capitalization.
Do not change the meaning, do not summarize, do not rephrase beyond that, and keep colloquial word
forms (mä, sä, meidän) as spoken. Keep every line, its timestamp and speaker label exactly, keep [?]
markers, and output the same number of lines and nothing else.

{verbatim}"""


def names_prompt(text: str, owner: str) -> str:
    owner_hint = f"The recording was made by {owner}, who is very likely one of the speakers. " if owner else ""
    return f"""Here is a meeting transcript with speakers labelled "Puhuja N". {owner_hint}For each label,
give the person's name ONLY if the transcript makes it clear (for example they introduce themselves, or
they are addressed by name and answer). Reply with a JSON object such as {{"Puhuja 1": "Etunimi Sukunimi"}}
and omit labels you are not sure about. Output JSON only.

{text}"""


def to_flac(ffmpeg: str, snippet: Path) -> Path:
    out_dir = snippet.parent / "gemini-flac"
    out_dir.mkdir(parents=True, exist_ok=True)
    flac = out_dir / f"{snippet.stem}.flac"
    if not flac.exists():
        r = subprocess.run([ffmpeg, "-hide_banner", "-y", "-i", str(snippet), "-ac", "1", "-ar", "16000",
                            "-c:a", "flac", str(flac)], capture_output=True, text=True)
        if r.returncode != 0:
            raise P.ProviderError(r.stderr.strip() or "ffmpeg FLAC conversion failed")
    return flac


RETRY_WAITS = (0, 15, 45)  # seconds before each attempt; covers short network drops


def pass_a(key: str, uri: str, langs: list, vocab: list, fallback_model: str) -> dict:
    """Verbatim + vocabulary. Up to three tries, then falls back to the general model."""
    for attempt, wait in enumerate(RETRY_WAITS):
        time.sleep(wait)
        try:
            text = P.gemini_interactions_text(
                P.gemini_interactions_raw(key, uri, "audio/flac", langs, "verbatim", vocab))
        except P.GeminiSpendCapReached:
            raise
        except P.GeminiModelLimited as exc:  # a per-day limit does not clear in seconds
            log(f"pass A: {exc}")
            break
        except Exception as exc:  # noqa: BLE001
            log(f"pass A attempt {attempt + 1} failed: {exc}")
            continue
        if text and not P.gemini_looks_degenerate(text):
            return {"text": text, "model": P.GEMINI_TRANSCRIBE_MODEL}
    if fallback_model:
        try:
            text = P.gemini_transcribe_generate(key, uri, "audio/flac", langs, fallback_model,
                                                custom_vocabulary=vocab)
            if text and not P.gemini_looks_degenerate(text):
                return {"text": text, "model": fallback_model}
        except P.GeminiSpendCapReached:
            raise
        except Exception as exc:  # noqa: BLE001
            log(f"pass A fallback failed: {exc}")
    return {"text": "", "model": "none"}


def pass_b(key: str, uri: str, langs: list) -> dict:
    """Verbatim + diarization + word timestamps. Up to three tries; empty result means 'no speakers'."""
    for attempt, wait in enumerate(RETRY_WAITS):
        time.sleep(wait)
        try:
            data = P.gemini_interactions_raw(key, uri, "audio/flac", langs, DIARIZE_MODE)
        except P.GeminiSpendCapReached:
            raise
        except P.GeminiModelLimited as exc:  # a per-day limit does not clear in seconds
            log(f"pass B: {exc}")
            break
        except Exception as exc:  # noqa: BLE001
            log(f"pass B attempt {attempt + 1} failed: {exc}")
            continue
        text = P.gemini_interactions_text(data)
        words = P.gemini_interactions_words(data)
        if text and words and not P.gemini_looks_degenerate(text):
            return {"text": text, "words": words}
    return {"text": "", "words": []}


# ---- EU Flash audio passes (Agent Platform, EU multi-region) ------------------------------------

FLASH_TURNS_SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {
            "start": {"type": "STRING", "description": "Turn start time from the beginning of THIS audio, mm:ss"},
            "speaker": {"type": "STRING", "description": "S1, S2, ... one label per distinct voice"},
            "text": {"type": "STRING", "description": "Verbatim words of the turn"},
        },
        "required": ["start", "speaker", "text"],
    },
}


def flash_plain_prompt(vocab: list) -> str:
    return P.flash_plain_prompt(vocab)  # shared with EU perustaso so Huipputaso can reuse it


def flash_turns_prompt(vocab: list) -> str:
    return (
        "Litteroi tämä suomenkielinen kokousäänite sanatarkasti puheenvuoroittain.\n"
        "- Aloita uusi puheenvuoro aina kun puhuja vaihtuu. Merkitse puhujat S1, S2, S3 ... niin että sama "
        "ääni saa aina saman tunnuksen koko äänitteen ajan. Älä päättele nimiä.\n"
        "- start = puheenvuoron alkuaika tämän äänitteen alusta muodossa mm:ss. Aikojen pitää kasvaa.\n"
        "- text = puheenvuoron sanat täsmälleen niin kuin ne sanotaan, myös puhekieli, täytesanat ja toistot. "
        "Englanninkieliset ilmaukset englanniksi. Epäselvä sana = [epäselvä].\n"
        "- Kata koko äänite alusta loppuun, älä jätä mitään pois äläkä toista jaksoja.\n"
        "Nimet ja termit, jotka voivat esiintyä: " + ("; ".join(vocab[:300]) or "-")
    )


def to_mp3(ffmpeg: str, snippet: Path) -> Path:
    return P.to_mp3(ffmpeg, snippet)


def mmss(value: str) -> float:
    parts = [p for p in re.split(r"[:.]", str(value or "0").strip()) if p.isdigit()]
    if not parts:
        return 0.0
    nums = [int(p) for p in parts[-3:]]
    secs = 0
    for n in nums:
        secs = secs * 60 + n
    return float(secs)


def pass_a_flash(ap: tuple, audio_part: dict, vocab: list) -> dict:
    project, location, model = ap
    for attempt, wait in enumerate(RETRY_WAITS):
        time.sleep(wait)
        try:
            text = P.ap_generate(project, location, model, [{"text": flash_plain_prompt(vocab)}, audio_part],
                                 temperature=0.0, max_output_tokens=65536, thinking_budget=1024)
        except Exception as exc:  # noqa: BLE001
            log(f"flash pass A attempt {attempt + 1} failed: {exc}")
            continue
        if text and not P.gemini_looks_degenerate(text):
            return {"text": text, "model": f"{location}:{model}"}
        log(f"flash pass A attempt {attempt + 1}: empty or degenerate output")
    return {"text": "", "model": "none"}


def pass_b_flash(ap: tuple, audio_part: dict, vocab: list) -> dict:
    project, location, model = ap
    for attempt, wait in enumerate(RETRY_WAITS):
        time.sleep(wait)
        try:
            raw = P.ap_generate(project, location, model, [{"text": flash_turns_prompt(vocab)}, audio_part],
                                temperature=0.0, max_output_tokens=65536, thinking_budget=1024,
                                response_schema=FLASH_TURNS_SCHEMA)
            items = json.loads(raw)
        except Exception as exc:  # noqa: BLE001
            log(f"flash pass B attempt {attempt + 1} failed: {exc}")
            continue
        turns = [{"speaker": f"spk:{str(i.get('speaker', '?')).strip()}", "start": mmss(i.get("start")),
                  "text": str(i.get("text", "")).strip()} for i in items if str(i.get("text", "")).strip()]
        joined = " ".join(t["text"] for t in turns)
        if turns and not P.gemini_looks_degenerate(joined):
            return {"text": joined, "turns": turns}
        log(f"flash pass B attempt {attempt + 1}: empty or degenerate output")
    return {"text": "", "turns": []}


def merge_segment(key: str, model: str, idx: int, offset: float, a: dict, b: dict, vocab: list,
                  roster: str, tail: str) -> dict:
    """Return {"lines": [...], "method": ...} for one snippet."""
    if b.get("turns"):
        turns = [{"speaker": t["speaker"], "start": offset + t["start"], "text": t["text"]} for t in b["turns"]]
    else:
        turns = build_turns(b.get("text", ""), b.get("words", []), offset)
    if not turns:
        text = a.get("text", "")
        lines = [{"time": hms(offset), "speaker": "Puhuja ?", "text": text}] if text else []
        return {"lines": lines, "method": "A only (no speaker data)"}
    fallback = [{"time": hms(t["start"]), "speaker": f"Puhuja {idx + 1}.{t['speaker'].split(':')[-1]}",
                 "text": t["text"]} for t in turns]
    if not a.get("text"):
        return {"lines": fallback, "method": "B only (no vocabulary pass)"}
    try:
        # maxOutputTokens includes thinking tokens: cap thinking so it cannot crowd out the transcript.
        out = _TEXT(merge_prompt(turns, a["text"], vocab, roster, tail),
                    temperature=0.0, max_output_tokens=65536, thinking_budget=2048)
    except P.GeminiSpendCapReached:
        raise
    except Exception as exc:  # noqa: BLE001
        log(f"merge failed for segment {idx}: {exc}")
        return {"lines": fallback, "method": "B only (merge failed)"}
    lines = parse_lines(out)
    b_words = sum(word_count(t["text"]) for t in turns)
    m_words = sum(word_count(l["text"]) for l in lines)
    if len(lines) < 0.8 * len(turns) or not (0.75 * b_words <= m_words <= 1.3 * b_words):
        log(f"merge rejected for segment {idx}: {len(lines)}/{len(turns)} lines, {m_words}/{b_words} words")
        return {"lines": fallback, "method": "B only (merge rejected by checks)", "raw_head": (out or "")[:1500]}
    return {"lines": lines, "method": "merged A+B"}


def clean_segment(key: str, model: str, lines: list) -> dict:
    if not lines:
        return {"lines": [], "method": "empty"}
    try:
        out = _TEXT(clean_prompt(format_lines(lines)), temperature=0.0,
                    max_output_tokens=65536, thinking_budget=0)
    except P.GeminiSpendCapReached:
        raise
    except Exception as exc:  # noqa: BLE001
        log(f"clean pass failed: {exc}")
        return {"lines": lines, "method": "verbatim (clean failed)"}
    cleaned = parse_lines(out)
    v_words = sum(word_count(l["text"]) for l in lines)
    c_words = sum(word_count(l["text"]) for l in cleaned)
    if len(cleaned) < 0.9 * len(lines) or c_words < 0.5 * v_words or c_words > 1.05 * v_words:
        log(f"clean pass rejected: {len(cleaned)}/{len(lines)} lines, {c_words}/{v_words} words")
        return {"lines": lines, "method": "verbatim (clean rejected by checks)"}
    return {"lines": cleaned, "method": "cleaned"}


def fix_times(lines: list, b: dict, offset: float, end=None) -> list:
    """Never trust timestamps written by the text model: take them from the speaker-pass turns when the
    line count matches, otherwise keep the model's times but force them to be non-decreasing."""
    if b.get("turns"):
        turns = [offset + t["start"] for t in b["turns"]]
    else:
        turns = [t["start"] for t in build_turns(b.get("text", ""), b.get("words", []), offset)]
    if turns and len(turns) == len(lines):
        lines = [dict(l, time=hms(t)) for l, t in zip(lines, turns)]
    # Speaker-pass word times can glitch too (seen: a turn stamped 00:02:10 inside minute 26), so clamp.
    out, prev = [], hms(offset)
    cap = hms(end) if end is not None else None
    for l in lines:
        t = l["time"] if l["time"] >= prev else prev
        if cap is not None and t > cap:
            t = max(prev, cap)  # a model-written time past the end of this snippet
        out.append(dict(l, time=t))
        prev = t
    return out


def roster_from(lines: list, previous: dict) -> dict:
    """Label -> first sample sentence, accumulated over segments."""
    roster = dict(previous)
    for l in lines:
        if l["speaker"] not in roster and l["speaker"].startswith("Puhuja ") and "." not in l["speaker"]:
            roster[l["speaker"]] = l["text"][:120]
    return roster


def infer_names(key: str, model: str, text: str, owner: str) -> dict:
    try:
        out = _TEXT(names_prompt(text[:200000], owner), temperature=0.0)
    except Exception as exc:  # noqa: BLE001
        log(f"name inference failed: {exc}")
        return {}
    m = re.search(r"\{.*\}", out or "", re.S)
    if not m:
        return {}
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return {}
    return {k: str(v).strip() for k, v in data.items()
            if re.fullmatch(r"Puhuja \d+", str(k)) and str(v).strip() and len(str(v)) <= 60}


def apply_names(lines: list, names: dict) -> list:
    return [dict(l, speaker=names.get(l["speaker"], l["speaker"])) for l in lines]


def snippet_offsets(snippets: list, cuts_file: Path, ffprobe) -> list:
    if cuts_file.exists():
        cuts = {c["file"]: float(c["start"]) for c in json.loads(cuts_file.read_text(encoding="utf-8"))}
        if all(s.name in cuts for s in snippets):
            return [cuts[s.name] for s in snippets]
    offsets, pos = [], 0.0
    for s in snippets:
        offsets.append(pos)
        pos += ffprobe(s) or 0.0
    return offsets


def run(config: dict, recording: Path, job_dir: Path, progress) -> str:
    """Run the tarkka pipeline, write outputs into job_dir, return the readable transcript text."""
    import transcribe_recording as TR

    global _TEXT
    _TEXT = P.text_generator(config)
    audio_backend = str(config.get("tarkka_audio_backend", "ai_studio")).lower()
    eu_flash = audio_backend == "eu_flash"
    key = P.gemini_key(None)
    if not key and not eu_flash:
        raise P.ProviderError("No Gemini API key set")
    ap = P.ap_settings(config)
    ap = (ap[0], ap[1], str(config.get("agent_platform_audio_model", ap[2])))
    langs = list(config.get("gemini_language_codes") or ["fi-FI"])
    vocab = P.load_vocabulary(config)
    text_model = getattr(_TEXT, "label", "?")
    fallback_model = str(config.get("gemini_transcribe_fallback_model", P.GEMINI_FALLBACK_MODEL) or "")
    owner = str(config.get("meeting_owner", "") or "")
    ffmpeg = P.ffmpeg_path(config)
    sdir = job_dir / ("snippets-tarkka-eu" if eu_flash else "snippets-tarkka")
    target = int(config.get("tarkka_eu_snippet_seconds", 600) if eu_flash else config.get("tarkka_snippet_seconds", 1500))
    max_len = int(config.get("tarkka_eu_snippet_max_seconds", 780) if eu_flash
                  else config.get("tarkka_snippet_max_seconds", 1740))
    snippets = TR.split_snippets(
        recording, sdir, target, progress,
        max_seconds=max_len, split_mode="silence",
        window_seconds=int(config.get("snippet_silence_window_seconds", 120)),
        silence_db=float(config.get("snippet_silence_db", -35.0)),
    )
    offsets = snippet_offsets(snippets, sdir / "cuts.json", TR.recording_duration_seconds)
    cache_dir = sdir / "transcripts"
    cache_dir.mkdir(parents=True, exist_ok=True)
    silence_db = float(config.get("silence_mean_db", TR.DEFAULT_SILENCE_MEAN_DB))

    def transcribe_one(i: int, snippet: Path) -> dict:
        cache = cache_dir / f"{snippet.stem}.passes.json"
        data: dict = {}
        if cache.exists():
            try:
                data = json.loads(cache.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
        def b_done(d):
            return bool(d.get("b", {}).get("words") or d.get("b", {}).get("turns"))

        if data.get("silent") or (data.get("a", {}).get("text") and b_done(data)):
            return data  # complete: nothing to pay for again
        if not data:
            mean_db = TR.snippet_mean_volume(snippet)
            if TR.snippet_is_silent(mean_db, silence_db):
                data = {"silent": True, "a": {"text": ""}, "b": {"text": "", "words": []}}
                cache.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
                return data
        # Run only the passes that are missing (e.g. after a network drop in an earlier run).
        if eu_flash:
            import base64

            mp3 = to_mp3(ffmpeg, snippet)
            audio_part = {"inlineData": {"mimeType": "audio/mpeg",
                                         "data": base64.b64encode(mp3.read_bytes()).decode("ascii")}}
            if not data.get("a", {}).get("text"):
                data["a"] = pass_a_flash(ap, audio_part, vocab)
            if not b_done(data):
                data["b"] = pass_b_flash(ap, audio_part, vocab)
        else:
            flac = to_flac(ffmpeg, snippet)
            uri = P.gemini_upload_file(key, flac, "audio/flac")
            if not data.get("a", {}).get("text"):
                data["a"] = pass_a(key, uri, langs, vocab, fallback_model)
            if not b_done(data):
                data["b"] = pass_b(key, uri, langs)
        cache.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        return data

    TR.write_progress(progress, stage="transcribing", progress=0.1,
                      message=f"Tarkka: transcribing {len(snippets)} snippets (2 passes each)")
    with ThreadPoolExecutor(max_workers=2) as pool:
        passes = list(pool.map(lambda args: transcribe_one(*args), enumerate(snippets)))

    segments: list = []
    roster: dict = {}
    tail = ""
    for i, (snippet, data) in enumerate(zip(snippets, passes)):
        TR.write_progress(progress, stage="transcribing", progress=round(0.5 + 0.2 * i / max(len(snippets), 1), 3),
                          message=f"Tarkka: merging snippet {i + 1}/{len(snippets)}")
        cache = cache_dir / f"{snippet.stem}.merged.json"
        source = (f"v3:{text_model}:{len(data['a'].get('text', ''))}:"
                  f"{len(data['b'].get('words', []))}:{len(data['b'].get('turns', []))}")
        merged = None
        if cache.exists():
            try:
                merged = json.loads(cache.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                merged = None
            if merged is not None and merged.get("source") != source:
                merged = None  # the passes changed since this merge (e.g. a missing pass was re-run)
        if merged is None:
            roster_text = "; ".join(f'{k} (e.g. "{v}")' for k, v in roster.items())
            merged = merge_segment(key, text_model, i, offsets[i], data["a"], data["b"], vocab, roster_text, tail)
            merged["source"] = source
            cache.write_text(json.dumps(merged, ensure_ascii=False, indent=1), encoding="utf-8")
        seg_end = offsets[i + 1] if i + 1 < len(offsets) else None
        merged = dict(merged, lines=fix_times(merged["lines"], data["b"], offsets[i], seg_end))
        roster = roster_from(merged["lines"], roster)
        tail = format_lines(merged["lines"][-6:])
        segments.append(merged)

    TR.write_progress(progress, stage="transcribing", progress=0.72, message="Tarkka: readable version")

    def clean_one(i: int) -> dict:
        cache = cache_dir / f"{snippets[i].stem}.clean.json"
        source = str(zlib.crc32(format_lines(segments[i]["lines"]).encode("utf-8")))
        if cache.exists():
            try:
                previous = json.loads(cache.read_text(encoding="utf-8"))
                if previous.get("source") == source:
                    return previous
            except (OSError, ValueError):
                pass
        result = clean_segment(key, text_model, segments[i]["lines"])
        result["source"] = source
        cache.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
        return result

    with ThreadPoolExecutor(max_workers=2) as pool:
        cleaned = list(pool.map(clean_one, range(len(segments))))

    for i, c in enumerate(cleaned):  # readable lines inherit the verbatim timestamps when they align 1:1
        if len(c["lines"]) == len(segments[i]["lines"]):
            cleaned[i] = dict(c, lines=[dict(l, time=v["time"]) for l, v in zip(c["lines"], segments[i]["lines"])])
    verbatim_lines = [l for s in segments for l in s["lines"]]
    readable_lines = [l for c in cleaned for l in c["lines"]]
    TR.write_progress(progress, stage="transcribing", progress=0.8, message="Tarkka: inferring speaker names")
    names = infer_names(key, text_model, format_lines(readable_lines), owner) if readable_lines else {}
    verbatim_lines = apply_names(verbatim_lines, names)
    readable_lines = apply_names(readable_lines, names)

    methods = [s["method"] for s in segments]
    header = [
        ("Tarkka litterointi: Gemini 3.5 Flash EU-alueella" if eu_flash else
         "Tarkka litterointi: Gemini 3.5 Transcribe")
        + ", kaksi ajoa (sanatarkka + sanalista, puhujat + aikaleimat), yhdistetty kielimallilla"
        + f" ({text_model}).",
        "Puhujanimet on päätelty tekstistä, tarkista ne: "
        + (", ".join(f"{k} = {v}" for k, v in names.items()) if names else "ei varmoja nimiä") + ".",
        "[?] = lähteet olivat eri mieltä, tarkista kuuntelemalla.",
    ]
    fallbacks = [f"jakso {i + 1}: {m}" for i, m in enumerate(methods) if m != "merged A+B"]
    if fallbacks:
        header.append("Huom: " + "; ".join(fallbacks) + ".")
    head = "\n".join(f"> {h}" for h in header)
    verbatim = format_lines(verbatim_lines)
    readable = format_lines(readable_lines)
    (job_dir / "transcript.md").write_text(f"# Litteraatti (tarkka, sanatarkka)\n\n{head}\n\n{verbatim}\n",
                                           encoding="utf-8")
    (job_dir / "transcript-luettava.md").write_text(f"# Litteraatti (tarkka, luettava)\n\n{head}\n\n{readable}\n",
                                                    encoding="utf-8")
    (job_dir / "transcript.txt").write_text(verbatim + "\n", encoding="utf-8")
    (job_dir / "tarkka.json").write_text(json.dumps({
        "language_codes": langs, "vocabulary_terms": len(vocab), "text_model": text_model,
        "audio_backend": audio_backend, "audio_model": (f"{ap[1]}:{ap[2]}" if eu_flash else P.GEMINI_TRANSCRIBE_MODEL),
        "snippets": [{"file": str(s), "offset": o, "method": m, "a_model": p["a"].get("model")}
                     for s, o, m, p in zip(snippets, offsets, methods, passes)],
        "names": names,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    manifest = {"recording": str(recording), "preset": "tarkka", "transcribe_model": P.GEMINI_TRANSCRIBE_MODEL,
                "text_model": text_model, "snippets": [str(s) for s in snippets],
                "outputs": {n: str(job_dir / n) for n in ("transcript.md", "transcript-luettava.md",
                                                          "transcript.txt", "tarkka.json", "summary.md")}}
    (job_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    return readable
