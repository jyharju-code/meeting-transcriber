#!/usr/bin/env python3
"""Transcription and summarization providers (Google only).

- Google AI Studio (API key): gemini-3.5-transcribe (Interactions API) and gemini-3.5-flash.
- Gemini Enterprise Agent Platform, EU endpoint only (gcloud ADC): gemini-3.5-flash / 3.8-flash.
- A local whisper.cpp transcriber is available as an explicit, offline option.

Which of these a meeting may use is decided by the location switch (policy.py, D1-D3 in
docs/PAATOKSET.md). Every outbound model call goes through gemini_http(), which records it in
the job's call log; nothing here falls back implicitly to a provider that was not named.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import zlib
from pathlib import Path
from typing import Any


DEFAULT_MODELS_DIR = "~/.meeting-transcriber/models"
DEFAULT_WHISPER_MODEL = "large-v3-turbo-q5_0"
DEFAULT_WHISPER_MODEL_URL = (
    "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/"
    "ggml-large-v3-turbo-q5_0.bin"
)

# Google Gemini (AI Studio). Outside the EEA/CH/UK the unpaid tier lets Google use
# submitted content to improve its products; for EEA/CH/UK users the paid-service
# data terms apply to all use (see ai.google.dev/gemini-api/terms).
GEMINI_BASE = "https://generativelanguage.googleapis.com"
GEMINI_TRANSCRIBE_MODEL = "gemini-3.5-transcribe"
GEMINI_FALLBACK_MODEL = "gemini-3.5-flash"
GEMINI_KEY_ENVS = ("GEMINI_API_KEY", "GOOGLE_API_KEY")


class ProviderError(RuntimeError):
    """Raised when a selected provider cannot run (missing key, binary, etc.)."""


def ffmpeg_path(config: dict[str, Any] | None = None) -> str:
    explicit = (config or {}).get("ffmpeg_path")
    if explicit:
        return str(explicit)
    return "/opt/homebrew/bin/ffmpeg" if Path("/opt/homebrew/bin/ffmpeg").exists() else "ffmpeg"


def _result_text(result: Any) -> str:
    text = getattr(result, "text", None)
    if isinstance(text, str):
        return text
    if isinstance(result, dict) and isinstance(result.get("text"), str):
        return result["text"]
    return str(result)


def _result_jsonable(result: Any) -> Any:
    if hasattr(result, "model_dump"):
        return result.model_dump()
    if isinstance(result, (dict, list)):
        return result
    return {"text": _result_text(result)}


# --------------------------------------------------------------------------- #
# Google Gemini helpers (stdlib urllib; no extra dependency)
# --------------------------------------------------------------------------- #

def gemini_key(key_env: str | None = None) -> str | None:
    for env in ([key_env] if key_env else GEMINI_KEY_ENVS):
        value = os.environ.get(env) if env else None
        if value:
            return value
    return None


def gemini_http(req: "urllib.request.Request", timeout: int, attempts: int = 6) -> bytes:
    """Send a request, retrying on 429/5xx with exponential backoff.

    The free tier rate-limits gemini-3.5-transcribe fairly tightly, so transient
    429s are expected under load; back off and retry rather than failing the job.
    """
    import urllib.error

    import policy

    delay = 8.0
    url, body = req.full_url, req.data if isinstance(req.data, bytes) else None
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = resp.read()
            policy.record_call(url, body, "ok")
            return data
        except urllib.error.HTTPError as exc:
            policy.record_call(url, body, f"HTTP {exc.code}")
            if exc.code == 429:
                body = ""
                try:
                    body = exc.read().decode("utf-8", "replace")
                except Exception:
                    pass
                if gemini_is_spend_cap(body):
                    # Waiting cannot help: the project's monthly cap blocks every model.
                    raise GeminiSpendCapReached(
                        "Gemini project spend cap reached; raise it at https://ai.studio/spend "
                        "(it resets on the 1st of each month, PST)."
                    ) from exc
                if gemini_is_model_daily_limit(body):
                    # e.g. "limit: 100 requests per day on Tier 1" for gemini-3.5-transcribe:
                    # minutes of backoff cannot help, the caller should switch model.
                    raise GeminiModelLimited(body[:300], gemini_retry_seconds(body)) from exc
            if exc.code in (429, 500, 503) and attempt < attempts - 1:
                time.sleep(delay)
                delay = min(delay * 2, 90.0)
                continue
            raise
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            policy.record_call(url, body, f"virhe: {type(exc).__name__}")
            raise


class GeminiSpendCapReached(ProviderError):
    """The Gemini project's monthly spend cap blocks all requests."""


def gemini_is_spend_cap(body: str) -> bool:
    text = body.casefold()
    return "spending cap" in text or "spend cap" in text


class GeminiModelLimited(ProviderError):
    """One model hit its per-day request limit; other models may still work."""

    def __init__(self, message: str, retry_seconds: float):
        super().__init__(message)
        self.retry_seconds = retry_seconds


def gemini_is_model_daily_limit(body: str) -> bool:
    text = body.casefold()
    return "per day" in text or "perday" in text


def gemini_retry_seconds(body: str, default: float = 3600.0) -> float:
    import re

    match = re.search(r"retry in\s+(?:(\d+)h)?\s*(?:(\d+)m)?\s*(?:(\d+(?:\.\d+)?)s)?", body)
    if not match or not any(match.groups()):
        return default
    hours, minutes, seconds = (float(g) if g else 0.0 for g in match.groups())
    return hours * 3600 + minutes * 60 + seconds


# The general model (gemini-3.5-flash) sometimes "thinks aloud" into the
# transcript when unsure of a word ("Let's listen to 2:03 again", quoted
# alternatives joined with "or" / "->") and then loops on one phrase.
GEMINI_META_RE = re.compile(
    r"let's (listen|write|review|check|transcribe|re-?listen)|is what it sounds like"
    r"|\" or \"|\" -> \"|->\s*\"",
    re.I,
)
UNRELIABLE_MARKER = "[epäselvä jakso: litterointi epäonnistui]"


def gemini_looks_degenerate(text: str) -> bool:
    """Detect unusable output: leaked reasoning, or a word/phrase repetition loop."""
    if GEMINI_META_RE.search(text):
        return True
    words = text.split()
    if len(words) < 40:
        return False
    run = best = 1
    for a, b in zip(words, words[1:]):
        run = run + 1 if a == b else 1
        best = max(best, run)
    if best >= 25:
        return True
    # Phrase loops: one 4-word sequence covering a large share of the text.
    grams: dict[str, int] = {}
    for i in range(len(words) - 3):
        key = " ".join(words[i:i + 4])
        grams[key] = grams.get(key, 0) + 1
    top = max(grams.values(), default=0)
    if top >= 8 and top * 4 / len(words) >= 0.3:
        return True
    # Highly compressible long text is a loop (normal speech compresses to ~0.45).
    raw = text.encode("utf-8")
    return len(raw) >= 400 and len(zlib.compress(raw)) / len(raw) < 0.2


def gemini_upload_file(api_key: str, path: Path, mime: str) -> str:
    data = path.read_bytes()
    req = urllib.request.Request(
        f"{GEMINI_BASE}/upload/v1beta/files?key={api_key}",
        data=data,
        headers={
            "X-Goog-Upload-Command": "start, upload, finalize",
            "X-Goog-Upload-Header-Content-Length": str(len(data)),
            "X-Goog-Upload-Header-Content-Type": mime,
            "Content-Type": mime,
        },
    )
    return json.loads(gemini_http(req, timeout=300).decode("utf-8"))["file"]["uri"]


def gemini_mode_payload(mode: Any) -> Any:
    """Config mode -> API mode. "smart" stays a string, "verbatim" becomes {"type": "verbatim"},
    a dict (e.g. verbatim + diarization) is passed through unchanged."""
    if isinstance(mode, dict):
        return mode
    if str(mode or "smart").lower() == "verbatim":
        return {"type": "verbatim"}
    return "smart"


def gemini_interactions_raw(api_key, file_uri, mime, language_codes, mode, custom_vocabulary=None) -> dict:
    """Call gemini-3.5-transcribe via the Interactions API and return the raw response.

    custom_vocabulary is ignored when the mode asks for diarization or timestamps,
    because the API does not allow combining them."""
    mode_payload = gemini_mode_payload(mode)
    tconf: dict[str, Any] = {"language_codes": list(language_codes or []), "mode": mode_payload}
    wants_words = isinstance(mode_payload, dict) and (
        mode_payload.get("diarization_mode") or mode_payload.get("timestamp_granularities")
    )
    if custom_vocabulary and not wants_words:
        tconf["custom_vocabulary"] = list(custom_vocabulary)[:1000]
    body = {
        "model": GEMINI_TRANSCRIBE_MODEL,
        "input": [{"type": "audio", "uri": file_uri, "mime_type": mime}],
        "generation_config": {"transcription_config": tconf},
    }
    req = urllib.request.Request(
        f"{GEMINI_BASE}/v1beta/interactions?key={api_key}",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    return json.loads(gemini_http(req, timeout=900).decode("utf-8"))


def gemini_interactions_text(data: dict) -> str:
    parts = [
        content["text"]
        for step in data.get("steps", []) if step.get("type") == "model_output"
        for content in step.get("content", []) if content.get("type") == "text" and content.get("text")
    ]
    return "".join(parts).strip()


def gemini_interactions_words(data: dict) -> list:
    """Word annotations [{text, start, end, speaker}] from a diarized/timestamped response."""
    words = []
    for step in data.get("steps", []):
        if step.get("type") != "model_output":
            continue
        for content in step.get("content", []):
            for ann in content.get("annotations") or []:
                if ann.get("type") != "word_info":
                    continue

                def secs(value):
                    try:
                        return float(str(value or "0").rstrip("s"))
                    except ValueError:
                        return 0.0

                words.append({
                    "text": ann.get("text", ""),
                    "start": secs(ann.get("start_offset")),
                    "end": secs(ann.get("end_offset")),
                    "speaker": ann.get("speaker") or "spk:?",
                    "start_index": ann.get("start_index"),
                    "end_index": ann.get("end_index"),
                })
    return words


def gemini_transcribe_interactions(api_key, file_uri, mime, language_codes, mode, custom_vocabulary=None) -> str:
    """Dedicated gemini-3.5-transcribe via the Interactions API."""
    data = gemini_interactions_raw(api_key, file_uri, mime, language_codes, mode, custom_vocabulary)
    return gemini_interactions_text(data)


def load_vocabulary(config: dict) -> list:
    """Custom vocabulary from config: inline list + one-term-per-line file (# = comment)."""
    terms: list = []
    for term in config.get("gemini_custom_vocabulary") or []:
        terms.append(str(term).strip())
    path = config.get("gemini_vocabulary_file")
    if path:
        p = Path(str(path)).expanduser()
        if p.exists():
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.split("#", 1)[0].strip()
                if line:
                    terms.append(line)
    seen = set()
    out = []
    for term in terms:
        if term and term.casefold() not in seen:
            seen.add(term.casefold())
            out.append(term)
    return out[:1000]


def gemini_transcribe_generate(api_key, file_uri, mime, language_codes, model, temperature: float = 0.0,
                               custom_vocabulary=None) -> str:
    """General multimodal model (e.g. gemini-3.5-flash) via generateContent."""
    langs = ", ".join(language_codes) if language_codes else "the spoken language"
    prompt = (
        f"Transcribe this meeting audio verbatim into clean text in {langs}. "
        "Output ONLY the words that are spoken, once, in order. Never add notes, "
        "analysis, timestamps, speaker labels, alternatives or commentary in any "
        "language, and never repeat a passage. If a word is unclear, write [epäselvä] "
        "and continue."
    )
    if custom_vocabulary:
        prompt += (
            " Names and terms that may occur (spell them exactly like this when heard): "
            + "; ".join(list(custom_vocabulary)[:300]) + "."
        )
    body = {
        "contents": [{"parts": [{"text": prompt}, {"file_data": {"mime_type": mime, "file_uri": file_uri}}]}],
        # Transcription needs no reasoning; thinking tokens were ~20 % of the bill.
        "generationConfig": {"temperature": temperature, "thinkingConfig": {"thinkingBudget": 0}},
    }
    req = urllib.request.Request(
        f"{GEMINI_BASE}/v1beta/models/{model}:generateContent?key={api_key}",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    data = json.loads(gemini_http(req, timeout=600).decode("utf-8"))
    candidates = data.get("candidates", [])
    if not candidates:
        return ""
    parts = candidates[0].get("content", {}).get("parts", [])
    return "".join(p.get("text", "") for p in parts).strip()


def gemini_generate_text(api_key: str, model: str, prompt: str, temperature: float = 0.2,
                         max_output_tokens=None, thinking_budget=None) -> str:
    gen: dict[str, Any] = {"temperature": temperature}
    if max_output_tokens:
        gen["maxOutputTokens"] = int(max_output_tokens)
    if thinking_budget is not None:
        gen["thinkingConfig"] = {"thinkingBudget": int(thinking_budget)}
    body = {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": gen}
    req = urllib.request.Request(
        f"{GEMINI_BASE}/v1beta/models/{model}:generateContent?key={api_key}",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    data = json.loads(gemini_http(req, timeout=600).decode("utf-8"))
    candidates = data.get("candidates", [])
    if not candidates:
        return ""
    parts = candidates[0].get("content", {}).get("parts", [])
    return "".join(p.get("text", "") for p in parts).strip()


# --------------------------------------------------------------------------- #
# Gemini Enterprise Agent Platform (formerly Vertex AI), EU multi-region endpoint.
# Auth: gcloud Application Default Credentials (gcloud auth application-default login).
# --------------------------------------------------------------------------- #

AP_HOSTS = {"eu": "aiplatform.eu.rep.googleapis.com"}  # EU only: see docs/PAATOKSET.md D1
_AP_TOKEN: dict[str, Any] = {"token": None, "expires": 0.0}


def ap_token() -> str:
    if _AP_TOKEN["token"] and time.time() < _AP_TOKEN["expires"]:
        return str(_AP_TOKEN["token"])
    gcloud = shutil.which("gcloud") or "/opt/homebrew/bin/gcloud"
    result = subprocess.run([gcloud, "auth", "application-default", "print-access-token"],
                            capture_output=True, text=True)
    token = result.stdout.strip()
    if result.returncode != 0 or not token:
        raise ProviderError("Agent Platform: no ADC token (run: gcloud auth application-default login)")
    _AP_TOKEN.update(token=token, expires=time.time() + 40 * 60)
    return token


def ap_generate(project: str, location: str, model: str, parts: list, temperature: float = 0.0,
                max_output_tokens=None, thinking_budget=None, response_schema=None) -> str:
    """generateContent on Agent Platform; returns the concatenated text of the first candidate."""
    host = AP_HOSTS.get(location)
    if not host:
        raise ProviderError(f"Agent Platform location '{location}' is not allowed (only: {', '.join(AP_HOSTS)})")
    url = f"https://{host}/v1/projects/{project}/locations/{location}/publishers/google/models/{model}:generateContent"
    gen: dict[str, Any] = {"temperature": temperature}
    if max_output_tokens:
        gen["maxOutputTokens"] = int(max_output_tokens)
    if thinking_budget is not None:
        gen["thinkingConfig"] = {"thinkingBudget": int(thinking_budget)}
    if response_schema:
        gen["responseMimeType"] = "application/json"
        gen["responseSchema"] = response_schema
    body = {"contents": [{"role": "user", "parts": parts}], "generationConfig": gen}
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), headers={
        "Authorization": f"Bearer {ap_token()}", "Content-Type": "application/json",
        "x-goog-user-project": project})
    from urllib.error import HTTPError

    try:
        data = json.loads(gemini_http(req, timeout=900).decode("utf-8"))
    except HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:400]
        except Exception:  # noqa: BLE001
            pass
        if exc.code == 400 and "thinking budget is not supported" in detail.lower() and thinking_budget is not None:
            # Some serving paths reject thinkingConfig; retry the same request without it.
            return ap_generate(project, location, model, parts, temperature, max_output_tokens, None,
                               response_schema)
        raise ProviderError(f"Agent Platform HTTP {exc.code}: {detail}") from exc
    candidates = data.get("candidates", [])
    if not candidates:
        return ""
    return "".join(p.get("text", "") for p in candidates[0].get("content", {}).get("parts", [])).strip()


def ap_settings(config: dict) -> tuple:
    return (str(config.get("agent_platform_project", "")), str(config.get("agent_platform_location", "eu")),
            str(config.get("agent_platform_text_model", GEMINI_FALLBACK_MODEL)))


def text_generator(config: dict):
    """Callable(prompt, temperature=0.0, max_output_tokens=None, thinking_budget=None) -> str.

    Text steps always try Agent Platform EU first. If it fails and a Gemini API key exists, the call
    falls back to AI Studio (allowed by D3; the call log lights the red lamp in EU mode)."""
    project, location, model = ap_settings(config)
    key = gemini_key(None)
    studio_model = str(config.get("tarkka_text_model") or config.get("gemini_summary_model") or GEMINI_FALLBACK_MODEL)

    def gen(prompt, temperature=0.0, max_output_tokens=None, thinking_budget=None):
        if project:
            try:
                return ap_generate(project, location, model, [{"text": prompt}], temperature,
                                   max_output_tokens, thinking_budget)
            except GeminiSpendCapReached:
                raise
            except Exception as exc:  # noqa: BLE001
                if not key:
                    raise
                print(f"providers: Agent Platform EU failed ({exc}); using AI Studio", file=sys.stderr)
        if not key:
            raise ProviderError("No text backend: neither Agent Platform project nor Gemini API key")
        return gemini_generate_text(key, studio_model, prompt, temperature, max_output_tokens, thinking_budget)
    gen.label = f"agent_platform:{location}:{model}" if project else f"gemini_api:{studio_model}"  # type: ignore[attr-defined]
    return gen


# Verbatim transcription prompt for the general model (EU perustaso and Huipputaso F-A share it, so
# the perustaso result is reused by Huipputaso instead of being paid for twice).
FLASH_PLAIN_PROMPT_VERSION = "flash_plain_v1"


def flash_plain_prompt(vocab: list) -> str:
    return (
        "Litteroi tämä suomenkielinen kokousäänite sanatarkasti (verbatim).\n"
        "- Kirjoita jokainen sana täsmälleen niin kuin se sanotaan, myös puhekieli (mä, sä, et, niinku), "
        "täytesanat, toistot ja keskenjääneet lauseet. Älä korjaa kielioppia, älä tiivistä, älä selitä.\n"
        "- Englanninkieliset sanat ja ilmaukset kirjoitetaan englanniksi niin kuin ne sanotaan, ei käännetä.\n"
        "- Numerot ja päivämäärät numeroina niin kuin puhuja ne sanoo (esim. 4.12., 19. päivä, 50 sivua).\n"
        "- Jos sana on epäselvä, kirjoita [epäselvä] ja jatka. Älä koskaan arvaa nimeä vapaasti.\n"
        "- Tulosta vain litteraatti: ei otsikoita, aikaleimoja, puhujamerkintöjä tai kommentteja. "
        "Älä toista mitään jaksoa.\n"
        "Nimet ja termit, jotka voivat esiintyä (kirjoita juuri näin, jos kuulet ne): "
        + ("; ".join(vocab[:300]) or "-")
    )


def to_mp3(ffmpeg: str, snippet: Path) -> Path:
    out_dir = snippet.parent / "flash-mp3"
    out_dir.mkdir(parents=True, exist_ok=True)
    mp3 = out_dir / f"{snippet.stem}.mp3"
    if not mp3.exists():
        r = subprocess.run([ffmpeg, "-hide_banner", "-y", "-i", str(snippet), "-ac", "1", "-ar", "16000",
                            "-c:a", "libmp3lame", "-b:a", "48k", str(mp3)], capture_output=True, text=True)
        if r.returncode != 0:
            raise ProviderError(r.stderr.strip() or "ffmpeg mp3 conversion failed")
    return mp3


# --------------------------------------------------------------------------- #
# Transcription
# --------------------------------------------------------------------------- #

class Transcriber:
    name = "base"
    model_label = ""
    max_parallel = 3
    supports_diarization = False
    diarize = False

    def available(self) -> bool:
        raise NotImplementedError

    def prepare(self) -> None:
        """One-time setup (e.g. model download). Override as needed."""

    def transcribe(self, snippet: Path) -> tuple[str, Any]:
        raise NotImplementedError


class WhisperCppTranscriber(Transcriber):
    """Local whisper.cpp (whisper-cli). The always-available floor."""

    max_parallel = 1  # one model instance; GPU is the bottleneck

    def __init__(self, name, model, models_dir, model_url=None, binary=None,
                 ffmpeg="ffmpeg", language="auto", threads=None, auto_download=True):
        self.name = name
        self.model = model
        self.model_label = f"whisper.cpp:{model}"
        self.models_dir = Path(models_dir).expanduser()
        self.model_url = model_url
        self._binary_hint = binary
        self.ffmpeg = ffmpeg
        self.language = language
        self.threads = threads
        self.auto_download = auto_download

    def _binary(self) -> str | None:
        candidates = []
        if self._binary_hint:
            candidates.append(self._binary_hint)
        candidates += [
            "/opt/homebrew/bin/whisper-cli",
            "/usr/local/bin/whisper-cli",
            "/opt/homebrew/bin/whisper-cpp",
        ]
        for candidate in candidates:
            if Path(candidate).expanduser().exists():
                return str(Path(candidate).expanduser())
        return shutil.which("whisper-cli") or shutil.which("whisper-cpp")

    def _model_file(self) -> Path:
        name = self.model
        filename = name if name.endswith(".bin") else f"ggml-{name}.bin"
        return self.models_dir / filename

    def available(self) -> bool:
        if self._binary() is None:
            return False
        if self._model_file().exists():
            return True
        # Available if we are allowed to fetch the model on first use.
        return bool(self.auto_download and self.model_url)

    def prepare(self) -> None:
        if self._binary() is None:
            raise ProviderError(
                "whisper.cpp not found. Install it with: brew install whisper-cpp "
                "(or run ./install_whisper.sh)."
            )
        model_file = self._model_file()
        if model_file.exists():
            return
        if not (self.auto_download and self.model_url):
            raise ProviderError(
                f"Whisper model missing: {model_file}. Run ./install_whisper.sh "
                "or set whisper_auto_download to true."
            )
        self.models_dir.mkdir(parents=True, exist_ok=True)
        tmp = model_file.with_suffix(".download")
        print(
            f"providers: downloading whisper model '{self.model}' -> {model_file}",
            file=sys.stderr,
        )
        result = subprocess.run(["curl", "-L", "--fail", "-o", str(tmp), self.model_url])
        if result.returncode != 0 or not tmp.exists():
            raise ProviderError(f"Failed to download whisper model from {self.model_url}")
        tmp.replace(model_file)

    def transcribe(self, snippet: Path) -> tuple[str, Any]:
        self.prepare()
        binary = self._binary()
        with tempfile.TemporaryDirectory() as tmp:
            wav = Path(tmp) / "audio.wav"
            conv = subprocess.run(
                [
                    self.ffmpeg, "-hide_banner", "-y", "-i", str(snippet),
                    "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(wav),
                ],
                capture_output=True, text=True,
            )
            if conv.returncode != 0:
                raise ProviderError(conv.stderr.strip() or "ffmpeg wav conversion failed")

            out_prefix = Path(tmp) / "out"
            cmd = [
                binary, "-m", str(self._model_file()), "-f", str(wav),
                "-l", self.language, "-otxt", "-of", str(out_prefix),
            ]
            if self.threads:
                cmd += ["-t", str(self.threads)]
            run = subprocess.run(cmd, capture_output=True, text=True)
            if run.returncode != 0:
                raise ProviderError(
                    run.stderr.strip() or run.stdout.strip() or "whisper-cli failed"
                )
            out_txt = out_prefix.with_suffix(".txt")
            text = out_txt.read_text(encoding="utf-8").strip() if out_txt.exists() else ""
            return text, {"text": text, "engine": self.model_label}


class GeminiTranscriber(Transcriber):
    """Google Gemini transcription with a per-snippet hybrid.

    Each snippet is tried with `model` first (default gemini-3.5-transcribe, the
    dedicated ASR model reached via the Interactions API) and falls back to
    `fallback_model` (default gemini-3.5-flash via generateContent) when the
    dedicated model returns empty output or a token-loop collapse, which it can do
    on the free tier. Snippets are the worker's ~180 s chunks, well within the
    dedicated model's reliable range.
    """

    max_parallel = 2  # keep the free-tier transcribe rate limit happy

    def __init__(self, name, model=None, fallback_model=GEMINI_FALLBACK_MODEL,
                 mode="smart", language_codes=None, key_env=None, ffmpeg="ffmpeg",
                 custom_vocabulary=None):
        self.name = name
        self.model = model or GEMINI_TRANSCRIBE_MODEL
        self.fallback_model = fallback_model or ""
        self.mode = mode or "smart"
        self.language_codes = language_codes or ["fi-FI"]
        self.custom_vocabulary = list(custom_vocabulary or [])
        self.key_env = key_env
        self.ffmpeg = ffmpeg
        self.model_label = self.model
        self._blocked_until: dict[str, float] = {}

    def available(self) -> bool:
        return bool(gemini_key(self.key_env))

    def _to_flac(self, snippet: Path, out_dir: Path) -> Path:
        out_dir.mkdir(parents=True, exist_ok=True)
        flac = out_dir / f"{snippet.stem}.flac"
        if flac.exists():
            return flac
        result = subprocess.run(
            [self.ffmpeg, "-hide_banner", "-y", "-i", str(snippet),
             "-ac", "1", "-ar", "16000", "-c:a", "flac", str(flac)],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise ProviderError(result.stderr.strip() or "ffmpeg FLAC conversion failed")
        return flac

    def _run(self, api_key: str, file_uri: str, model: str, retry: bool = False) -> str:
        if model == GEMINI_TRANSCRIBE_MODEL:
            return gemini_transcribe_interactions(api_key, file_uri, "audio/flac", self.language_codes, self.mode,
                                                  self.custom_vocabulary)
        # A second try at a slightly higher temperature escapes a deterministic loop.
        return gemini_transcribe_generate(api_key, file_uri, "audio/flac", self.language_codes, model,
                                          temperature=0.3 if retry else 0.0,
                                          custom_vocabulary=self.custom_vocabulary)

    def transcribe(self, snippet: Path) -> tuple[str, Any]:
        api_key = gemini_key(self.key_env)
        if not api_key:
            raise ProviderError(f"No Gemini API key set for provider '{self.name}'")
        flac = self._to_flac(snippet, snippet.parent / "gemini-flac")
        attempts = [self.model, self.model]
        if self.fallback_model and self.fallback_model != self.model:
            attempts += [self.fallback_model, self.fallback_model]
        import urllib.error

        text = ""
        used = "none"
        last_error: Exception | None = None
        answered = False  # some model responded without error (possibly empty = silence)
        unreliable = False  # a model answered with leaked reasoning or a loop
        tried: dict[str, int] = {}
        for model in attempts:
            if self._blocked_until.get(model, 0.0) > time.time():
                continue  # this model hit its daily limit earlier in the job
            try:
                file_uri = gemini_upload_file(api_key, flac, "audio/flac")
                candidate = self._run(api_key, file_uri, model, retry=tried.get(model, 0) > 0)
                tried[model] = tried.get(model, 0) + 1
            except GeminiSpendCapReached:
                raise
            except GeminiModelLimited as exc:
                self._blocked_until[model] = time.time() + exc.retry_seconds
                last_error = exc
                print(f"providers: {model} daily limit reached; using fallback for this job", file=sys.stderr)
                continue
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                # A per-model rate limit or outage: move on to the next model.
                last_error = exc
                print(f"providers: {model} failed for {snippet.name}: {exc}", file=sys.stderr)
                continue
            answered = True
            if not candidate:
                if model != self.model:
                    break  # the fallback heard nothing: accept silence, no extra paid call
                continue  # the dedicated model can return empty on audio it fails on
            if gemini_looks_degenerate(candidate):
                unreliable = True
                continue
            text, used = candidate, model
            break
        if not text and not answered and last_error is not None:
            # Fail the job (so it is retried later) rather than saving a hole.
            raise ProviderError(f"Gemini could not transcribe {snippet.name}: {last_error}")
        if not text and unreliable:
            # Never pass leaked reasoning or a loop into the transcript; mark the gap.
            print(f"providers: no reliable transcript for {snippet.name}; marked as unclear", file=sys.stderr)
            text, used = UNRELIABLE_MARKER, "unreliable"
        elif not text:
            print(f"providers: Gemini produced no usable transcript for {snippet.name}", file=sys.stderr)
        return text, {"text": text, "model": used, "engine": f"gemini:{used}",
                      "mode": str(self.mode), "vocabulary": bool(self.custom_vocabulary)}


class AgentPlatformTranscriber(Transcriber):
    """EU perustaso: one verbatim pass with the general model on Agent Platform EU.

    Same prompt and settings as Huipputaso pass F-A, so Huipputaso reuses this result. If the EU
    service fails for a snippet and `fallback` is set, that snippet is transcribed by the fallback
    (AI Studio); D3 allows this and the call log lights the red lamp."""

    max_parallel = 3

    def __init__(self, name, project, location="eu", model=GEMINI_FALLBACK_MODEL, ffmpeg="ffmpeg",
                 custom_vocabulary=None, fallback: "Transcriber | None" = None):
        self.name = name
        self.project = project
        self.location = location
        self.model = model or GEMINI_FALLBACK_MODEL
        self.ffmpeg = ffmpeg
        self.custom_vocabulary = list(custom_vocabulary or [])
        self.fallback = fallback
        self.model_label = f"agent_platform:{location}:{self.model}"

    def available(self) -> bool:
        return bool(self.project) and bool(shutil.which("gcloud") or Path("/opt/homebrew/bin/gcloud").exists())

    def transcribe(self, snippet: Path) -> tuple[str, Any]:
        import base64

        mp3 = to_mp3(self.ffmpeg, snippet)
        part = {"inlineData": {"mimeType": "audio/mpeg", "data": base64.b64encode(mp3.read_bytes()).decode("ascii")}}
        prompt = flash_plain_prompt(self.custom_vocabulary)
        last_error: Exception | None = None
        unreliable = False
        for attempt, temperature in enumerate((0.0, 0.0, 0.3)):
            if attempt:
                time.sleep(10 * attempt)
            try:
                text = ap_generate(self.project, self.location, self.model, [{"text": prompt}, part],
                                   temperature=temperature, max_output_tokens=65536, thinking_budget=1024)
            except GeminiSpendCapReached:
                raise
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                print(f"providers: EU transcription attempt {attempt + 1} failed for {snippet.name}: {exc}",
                      file=sys.stderr)
                continue
            if text and gemini_looks_degenerate(text):
                unreliable = True
                continue
            return text, {"text": text, "model": self.model_label, "engine": self.model_label,
                          "prompt": FLASH_PLAIN_PROMPT_VERSION}
        if self.fallback is not None and self.fallback.available():
            print(f"providers: EU service failed for {snippet.name}; using {self.fallback.name}", file=sys.stderr)
            text, raw = self.fallback.transcribe(snippet)
            raw = dict(raw if isinstance(raw, dict) else {}, eu_fallback=True)
            return text, raw
        if last_error is not None and not unreliable:
            raise ProviderError(f"Agent Platform EU could not transcribe {snippet.name}: {last_error}")
        return UNRELIABLE_MARKER, {"text": UNRELIABLE_MARKER, "model": "unreliable", "engine": self.model_label}


# --------------------------------------------------------------------------- #
# Summarization
# --------------------------------------------------------------------------- #

class Summarizer:
    name = "base"
    model_label = ""

    def available(self) -> bool:
        raise NotImplementedError

    def summarize(self, prompt: str) -> str:
        raise NotImplementedError


class AgentPlatformSummarizer(Summarizer):
    """Gemini via Agent Platform (EU endpoint by default), authenticated with gcloud ADC."""

    def __init__(self, name, project, location="eu", model=GEMINI_FALLBACK_MODEL):
        self.name = name
        self.project = project
        self.location = location
        self.model = model or GEMINI_FALLBACK_MODEL
        self.model_label = f"agent_platform:{location}:{self.model}"

    def available(self) -> bool:
        return bool(self.project) and bool(shutil.which("gcloud") or Path("/opt/homebrew/bin/gcloud").exists())

    def summarize(self, prompt: str) -> str:
        return ap_generate(self.project, self.location, self.model, [{"text": prompt}], temperature=0.2)


class GeminiSummarizer(Summarizer):
    """Google Gemini summaries via generateContent (no OpenAI SDK needed)."""

    def __init__(self, name, model=GEMINI_FALLBACK_MODEL, key_env=None):
        self.name = name
        self.model = model or GEMINI_FALLBACK_MODEL
        self.model_label = self.model
        self.key_env = key_env

    def available(self) -> bool:
        return bool(gemini_key(self.key_env))

    def summarize(self, prompt: str) -> str:
        api_key = gemini_key(self.key_env)
        if not api_key:
            raise ProviderError(f"No Gemini API key set for provider '{self.name}'")
        return gemini_generate_text(api_key, self.model, prompt)


# --------------------------------------------------------------------------- #
# Registry + selection
# --------------------------------------------------------------------------- #

def default_providers(config: dict[str, Any]) -> dict[str, Any]:
    """Synthesized registry used when config has no explicit `providers` block.

    Google only (OpenAI was removed, docs/PAATOKSET.md D3), plus a local Whisper option.
    """
    return {
        "local_whisper": {
            "transcribe": {
                "type": "whisper_cpp",
                "model": config.get("whisper_model", DEFAULT_WHISPER_MODEL),
                "model_url": config.get("whisper_model_url", DEFAULT_WHISPER_MODEL_URL),
            }
        },
        "gemini": {
            "key_env": "GEMINI_API_KEY",
            "transcribe": {
                "type": "gemini",
                "model": config.get("gemini_transcribe_model", GEMINI_TRANSCRIBE_MODEL),
                "fallback_model": config.get("gemini_transcribe_fallback_model", GEMINI_FALLBACK_MODEL),
                "mode": config.get("gemini_transcribe_mode", "smart"),
                "language_codes": config.get("gemini_language_codes", ["fi-FI"]),
            },
            "summarize": {
                "type": "gemini",
                "model": config.get("gemini_summary_model", GEMINI_FALLBACK_MODEL),
            },
        },
    }


def build_transcribers(config: dict[str, Any]) -> dict[str, Transcriber]:
    providers = config.get("providers") or default_providers(config)
    models_dir = config.get("models_dir", DEFAULT_MODELS_DIR)
    diarize = config.get("transcribe_output_format") == "diarized_json"
    max_parallel = int(config.get("max_parallel_transcriptions", 3))
    out: dict[str, Transcriber] = {}
    for name, spec in providers.items():
        block = (spec or {}).get("transcribe")
        if not block:
            continue
        kind = block.get("type")
        if kind == "whisper_cpp":
            out[name] = WhisperCppTranscriber(
                name=name,
                model=block.get("model", DEFAULT_WHISPER_MODEL),
                models_dir=models_dir,
                model_url=block.get("model_url", DEFAULT_WHISPER_MODEL_URL),
                binary=config.get("whisper_binary"),
                ffmpeg=ffmpeg_path(config),
                language=block.get("language", config.get("whisper_language", "auto")),
                threads=block.get("threads"),
                auto_download=config.get("whisper_auto_download", True),
            )
        elif kind == "gemini":
            model = block.get("model", GEMINI_TRANSCRIBE_MODEL)
            fallback_model = block.get("fallback_model", GEMINI_FALLBACK_MODEL)
            if name == "gemini":
                model = config.get("gemini_transcribe_model", model)
                fallback_model = config.get("gemini_transcribe_fallback_model", fallback_model)
            out[name] = GeminiTranscriber(
                name=name,
                model=model,
                fallback_model=fallback_model,
                mode=block.get("mode", "smart"),
                language_codes=block.get("language_codes", ["fi-FI"]),
                key_env=spec.get("key_env"),
                ffmpeg=ffmpeg_path(config),
                custom_vocabulary=load_vocabulary(config),
            )
    return out


def build_summarizers(config: dict[str, Any]) -> dict[str, Summarizer]:
    providers = config.get("providers") or default_providers(config)
    out: dict[str, Summarizer] = {}
    for name, spec in providers.items():
        block = (spec or {}).get("summarize")
        if not block:
            continue
        if block.get("type") == "gemini":
            model = block.get("model", GEMINI_FALLBACK_MODEL)
            if name == "gemini":
                model = config.get("gemini_summary_model", model)
            out[name] = GeminiSummarizer(
                name=name,
                model=model,
                key_env=spec.get("key_env"),
            )
    if config.get("agent_platform_project"):
        project, location, model = ap_settings(config)
        out["agent_platform"] = AgentPlatformSummarizer("agent_platform", project, location, model)
    return out


def select(providers_map: dict[str, Any], primary: str | None, fallback: list[str] | None):
    """Return the first available provider in [primary, *fallback], else None.

    Only the providers named here are ever used; there is no implicit "any other available"
    fallback (it used to pick up OpenAI whenever its key happened to be set)."""
    order: list[str] = []
    for name in [primary, *(fallback or [])]:
        if name and name not in order:
            order.append(name)
    for name in order:
        provider = providers_map.get(name)
        if provider is not None and provider.available():
            return provider
    return None
