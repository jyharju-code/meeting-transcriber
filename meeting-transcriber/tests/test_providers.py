"""Unit tests for the provider registry, selection, and fallback.

No network, no OpenAI SDK, no whisper binary required.
"""

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import providers  # noqa: E402


class _Fake:
    def __init__(self, name, ok):
        self.name = name
        self._ok = ok
        self.model_label = name

    def available(self):
        return self._ok


class SelectTests(unittest.TestCase):
    def test_picks_primary_when_available(self):
        m = {"a": _Fake("a", True), "b": _Fake("b", True)}
        self.assertEqual(providers.select(m, "a", ["b"]).name, "a")

    def test_falls_back_when_primary_unavailable(self):
        m = {"a": _Fake("a", False), "b": _Fake("b", True)}
        self.assertEqual(providers.select(m, "a", ["b"]).name, "b")

    def test_falls_back_to_any_available_when_chain_exhausted(self):
        m = {"a": _Fake("a", False), "c": _Fake("c", True)}
        self.assertEqual(providers.select(m, "a", []).name, "c")

    def test_returns_none_when_nothing_available(self):
        m = {"a": _Fake("a", False), "b": _Fake("b", False)}
        self.assertIsNone(providers.select(m, "a", ["b"]))

    def test_ignores_unknown_names(self):
        m = {"a": _Fake("a", True)}
        self.assertEqual(providers.select(m, "missing", ["a"]).name, "a")


class OpenAIChatSummarizerTests(unittest.TestCase):
    def test_available_requires_key(self):
        s = providers.OpenAIChatSummarizer("openai", "https://x/v1", "OPENAI_API_KEY", "gpt-4o-mini")
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(s.available())
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}, clear=True):
            self.assertTrue(s.available())

    def test_keyless_provider_is_always_available(self):
        s = providers.OpenAIChatSummarizer("ollama", "http://localhost:11434/v1", None, "qwen2.5:7b")
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertTrue(s.available())


class OpenAIAudioTranscriberTests(unittest.TestCase):
    def test_diarize_wiring(self):
        t = providers.OpenAIAudioTranscriber(
            "openai", "https://x/v1", "OPENAI_API_KEY",
            model="gpt-4o-mini-transcribe", diarize_model="gpt-4o-transcribe-diarize", diarize=True,
        )
        self.assertTrue(t.supports_diarization)
        self.assertTrue(t.diarize)
        self.assertEqual(t.model, "gpt-4o-transcribe-diarize")
        self.assertEqual(t.response_format, "diarized_json")

    def test_plain_wiring(self):
        t = providers.OpenAIAudioTranscriber(
            "openai", "https://x/v1", "OPENAI_API_KEY",
            model="gpt-4o-mini-transcribe", diarize_model="gpt-4o-transcribe-diarize", diarize=False,
        )
        self.assertFalse(t.diarize)
        self.assertEqual(t.model, "gpt-4o-mini-transcribe")
        self.assertEqual(t.response_format, "json")

    def test_available_requires_key(self):
        t = providers.OpenAIAudioTranscriber("openai", None, "OPENAI_API_KEY", "gpt-4o-mini-transcribe")
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(t.available())
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}, clear=True):
            self.assertTrue(t.available())


class WhisperCppTranscriberTests(unittest.TestCase):
    def test_floor_traits(self):
        t = providers.WhisperCppTranscriber("local_whisper", "large-v3-turbo-q5_0", "/tmp/models")
        self.assertEqual(t.max_parallel, 1)
        self.assertFalse(t.supports_diarization)

    def test_model_filename_mapping(self):
        t = providers.WhisperCppTranscriber("local_whisper", "large-v3-turbo-q5_0", "/tmp/models")
        self.assertTrue(str(t._model_file()).endswith("ggml-large-v3-turbo-q5_0.bin"))
        t2 = providers.WhisperCppTranscriber("local_whisper", "custom.bin", "/tmp/models")
        self.assertTrue(str(t2._model_file()).endswith("custom.bin"))

    def test_unavailable_without_binary(self):
        t = providers.WhisperCppTranscriber(
            "local_whisper", "large-v3-turbo-q5_0", "/tmp/models", binary="/definitely/not/here"
        )
        with mock.patch.object(providers.shutil, "which", return_value=None), \
             mock.patch.object(providers.Path, "exists", return_value=False):
            self.assertFalse(t.available())


class RegistryBuildTests(unittest.TestCase):
    def test_default_registry_has_local_and_openai(self):
        transcribers = providers.build_transcribers({})
        self.assertIn("local_whisper", transcribers)
        self.assertIn("openai", transcribers)
        summarizers = providers.build_summarizers({})
        self.assertIn("openai", summarizers)

    def test_explicit_providers_block_is_used(self):
        config = {
            "transcribe_output_format": "md",
            "providers": {
                "openrouter": {
                    "base_url": "https://openrouter.ai/api/v1",
                    "key_env": "OPENROUTER_API_KEY",
                    "summarize": {"type": "openai_chat", "model": "anthropic/claude-3.5-sonnet"},
                }
            },
        }
        summarizers = providers.build_summarizers(config)
        self.assertEqual(list(summarizers), ["openrouter"])
        self.assertEqual(summarizers["openrouter"].model, "anthropic/claude-3.5-sonnet")
        # No transcribe block -> no transcribers from this registry.
        self.assertEqual(providers.build_transcribers(config), {})


if __name__ == "__main__":
    unittest.main()
