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

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


DEFAULT_MODELS_DIR = "~/.meeting-transcriber/models"
DEFAULT_WHISPER_MODEL = "large-v3-turbo-q5_0"
DEFAULT_WHISPER_MODEL_URL = (
    "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/"
    "ggml-large-v3-turbo-q5_0.bin"
)


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
