#!/usr/bin/env python3
"""Pluggable transcription and summarization providers.

Design goals (v1):
- A local Whisper (whisper.cpp) transcriber is the always-available floor:
  no API key, no network at transcription time once the model is cached.
- API providers are unlocked by setting their key in the environment. They are
  selected by name in config, with a fallback chain that always ends at local.
- "Not just ChatGPT": any OpenAI-compatible chat endpoint works by configuration
  alone (OpenAI, OpenRouter, Groq, Mistral, a local Ollama server, ...). The
  OpenAI SDK's `base_url` is the single mechanism; no extra dependencies.

The OpenAI SDK is imported lazily inside provider methods so this module (and
its unit tests) import cleanly without the package installed.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any


DEFAULT_MODELS_DIR = "~/.meeting-transcriber/models"
DEFAULT_WHISPER_MODEL = "large-v3-turbo-q5_0"
DEFAULT_WHISPER_MODEL_URL = (
    "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/"
    "ggml-large-v3-turbo-q5_0.bin"
)

# Google Gemini (AI Studio). Free tier is 0 EUR in exchange for Google using the
# submitted audio/text to improve its products.
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

    delay = 8.0
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 500, 503) and attempt < attempts - 1:
                time.sleep(delay)
                delay = min(delay * 2, 90.0)
                continue
            raise


def gemini_looks_degenerate(text: str) -> bool:
    """Detect ASR collapse where one token repeats consecutively many times."""
    words = text.split()
    if len(words) < 40:
        return False
    run = best = 1
    for a, b in zip(words, words[1:]):
        run = run + 1 if a == b else 1
        best = max(best, run)
    return best >= 25


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


def gemini_transcribe_interactions(api_key, file_uri, mime, language_codes, mode) -> str:
    """Dedicated gemini-3.5-transcribe via the Interactions API."""
    body = {
        "model": GEMINI_TRANSCRIBE_MODEL,
        "input": [{"type": "audio", "uri": file_uri, "mime_type": mime}],
        "generation_config": {"transcription_config": {"language_codes": language_codes, "mode": mode}},
    }
    req = urllib.request.Request(
        f"{GEMINI_BASE}/v1beta/interactions?key={api_key}",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    data = json.loads(gemini_http(req, timeout=600).decode("utf-8"))
    parts = [
        content["text"]
        for step in data.get("steps", []) if step.get("type") == "model_output"
        for content in step.get("content", []) if content.get("type") == "text" and content.get("text")
    ]
    return "".join(parts).strip()


def gemini_transcribe_generate(api_key, file_uri, mime, language_codes, model) -> str:
    """General multimodal model (e.g. gemini-3.5-flash) via generateContent."""
    langs = ", ".join(language_codes) if language_codes else "the spoken language"
    prompt = (
        f"Transcribe this meeting audio verbatim into clean text in {langs}. "
        "Output only the transcript, with no timestamps, speaker labels, or commentary."
    )
    body = {
        "contents": [{"parts": [{"text": prompt}, {"file_data": {"mime_type": mime, "file_uri": file_uri}}]}],
        "generationConfig": {"temperature": 0},
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


def gemini_generate_text(api_key: str, model: str, prompt: str) -> str:
    body = {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"temperature": 0.2}}
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


class OpenAIAudioTranscriber(Transcriber):
    """OpenAI-compatible /audio/transcriptions (OpenAI, Groq, ...)."""

    def __init__(self, name, base_url, key_env, model, diarize_model=None,
                 diarize=False, max_parallel=3):
        self.name = name
        self.base_url = base_url
        self.key_env = key_env or "OPENAI_API_KEY"
        self.supports_diarization = bool(diarize_model)
        self.diarize = bool(diarize and diarize_model)
        self.model = diarize_model if self.diarize else model
        self.model_label = self.model
        self.response_format = "diarized_json" if self.diarize else "json"
        self.max_parallel = max_parallel

    def available(self) -> bool:
        return bool(os.environ.get(self.key_env))

    def _client(self):
        from openai import OpenAI

        key = os.environ.get(self.key_env)
        if not key:
            raise ProviderError(f"{self.key_env} is not set for provider '{self.name}'")
        kwargs: dict[str, Any] = {"api_key": key}
        if self.base_url:
            kwargs["base_url"] = self.base_url
        return OpenAI(**kwargs)

    def transcribe(self, snippet: Path) -> tuple[str, Any]:
        client = self._client()
        with snippet.open("rb") as audio_file:
            result = client.audio.transcriptions.create(
                file=audio_file,
                model=self.model,
                response_format=self.response_format,
            )
        return _result_text(result), _result_jsonable(result)


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
                 mode="smart", language_codes=None, key_env=None, ffmpeg="ffmpeg"):
        self.name = name
        self.model = model or GEMINI_TRANSCRIBE_MODEL
        self.fallback_model = fallback_model or ""
        self.mode = mode or "smart"
        self.language_codes = language_codes or ["fi-FI"]
        self.key_env = key_env
        self.ffmpeg = ffmpeg
        self.model_label = self.model

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

    def _run(self, api_key: str, file_uri: str, model: str) -> str:
        if model == GEMINI_TRANSCRIBE_MODEL:
            return gemini_transcribe_interactions(api_key, file_uri, "audio/flac", self.language_codes, self.mode)
        return gemini_transcribe_generate(api_key, file_uri, "audio/flac", self.language_codes, model)

    def transcribe(self, snippet: Path) -> tuple[str, Any]:
        api_key = gemini_key(self.key_env)
        if not api_key:
            raise ProviderError(f"No Gemini API key set for provider '{self.name}'")
        flac = self._to_flac(snippet, snippet.parent / "gemini-flac")
        attempts = [self.model, self.model]
        if self.fallback_model and self.fallback_model != self.model:
            attempts.append(self.fallback_model)
        text = ""
        used = "none"
        for model in attempts:
            file_uri = gemini_upload_file(api_key, flac, "audio/flac")
            candidate = self._run(api_key, file_uri, model)
            if candidate and not gemini_looks_degenerate(candidate):
                text, used = candidate, model
                break
        if not text:
            print(f"providers: Gemini produced no usable transcript for {snippet.name}", file=sys.stderr)
        return text, {"text": text, "model": used, "engine": f"gemini:{used}"}


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


class OpenAIChatSummarizer(Summarizer):
    """Any OpenAI-compatible /chat/completions endpoint.

    Works for OpenAI, OpenRouter (-> Claude/Gemini/Llama/...), Groq, Mistral,
    and a local Ollama server, selected purely by base_url + model in config.
    """

    def __init__(self, name, base_url, key_env, model, extra_headers=None):
        self.name = name
        self.base_url = base_url
        self.key_env = key_env  # may be None for a keyless local server
        self.model = model
        self.model_label = model
        self.extra_headers = extra_headers or {}

    def available(self) -> bool:
        if not self.key_env:
            return True  # keyless (e.g. local Ollama)
        return bool(os.environ.get(self.key_env))

    def _client(self):
        from openai import OpenAI

        kwargs: dict[str, Any] = {}
        if self.key_env:
            key = os.environ.get(self.key_env)
            if not key:
                raise ProviderError(f"{self.key_env} is not set for provider '{self.name}'")
            kwargs["api_key"] = key
        else:
            kwargs["api_key"] = os.environ.get("OPENAI_API_KEY", "not-needed")
        if self.base_url:
            kwargs["base_url"] = self.base_url
        if self.extra_headers:
            kwargs["default_headers"] = self.extra_headers
        return OpenAI(**kwargs)

    def summarize(self, prompt: str) -> str:
        client = self._client()
        response = client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
        )
        return (response.choices[0].message.content or "").strip()


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

    Keeps legacy configs (no providers block) working: OpenAI for both stages
    using the old top-level model keys, plus a local Whisper option.
    """
    return {
        "local_whisper": {
            "transcribe": {
                "type": "whisper_cpp",
                "model": config.get("whisper_model", DEFAULT_WHISPER_MODEL),
                "model_url": config.get("whisper_model_url", DEFAULT_WHISPER_MODEL_URL),
            }
        },
        "openai": {
            "base_url": "https://api.openai.com/v1",
            "key_env": "OPENAI_API_KEY",
            "transcribe": {
                "type": "openai_audio",
                "model": config.get("transcribe_model", "gpt-4o-mini-transcribe"),
                "diarize_model": config.get("diarize_model", "gpt-4o-transcribe-diarize"),
            },
            "summarize": {
                "type": "openai_chat",
                "model": config.get("summary_model", "gpt-4o-mini"),
            },
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
        elif kind == "openai_audio":
            out[name] = OpenAIAudioTranscriber(
                name=name,
                base_url=spec.get("base_url"),
                key_env=spec.get("key_env", "OPENAI_API_KEY"),
                model=block.get("model", "gpt-4o-mini-transcribe"),
                diarize_model=block.get("diarize_model"),
                diarize=diarize,
                max_parallel=max_parallel,
            )
        elif kind == "gemini":
            out[name] = GeminiTranscriber(
                name=name,
                model=block.get("model", GEMINI_TRANSCRIBE_MODEL),
                fallback_model=block.get("fallback_model", GEMINI_FALLBACK_MODEL),
                mode=block.get("mode", "smart"),
                language_codes=block.get("language_codes", ["fi-FI"]),
                key_env=spec.get("key_env"),
                ffmpeg=ffmpeg_path(config),
            )
    return out


def build_summarizers(config: dict[str, Any]) -> dict[str, Summarizer]:
    providers = config.get("providers") or default_providers(config)
    out: dict[str, Summarizer] = {}
    for name, spec in providers.items():
        block = (spec or {}).get("summarize")
        if not block:
            continue
        if block.get("type") == "openai_chat":
            out[name] = OpenAIChatSummarizer(
                name=name,
                base_url=spec.get("base_url"),
                key_env=spec.get("key_env"),
                model=block.get("model", "gpt-4o-mini"),
                extra_headers=spec.get("extra_headers"),
            )
        elif block.get("type") == "gemini":
            out[name] = GeminiSummarizer(
                name=name,
                model=block.get("model", GEMINI_FALLBACK_MODEL),
                key_env=spec.get("key_env"),
            )
    return out


def select(providers_map: dict[str, Any], primary: str | None, fallback: list[str] | None):
    """Return the first available provider in [primary, *fallback, *rest], else None."""
    order: list[str] = []
    for name in [primary, *(fallback or [])]:
        if name and name not in order:
            order.append(name)
    for name in providers_map:
        if name not in order:
            order.append(name)
    for name in order:
        provider = providers_map.get(name)
        if provider is not None and provider.available():
            return provider
    return None
