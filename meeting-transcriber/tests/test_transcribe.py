"""Unit tests for the transcription worker's pure helpers.

`transcribe_recording` imports the OpenAI SDK lazily (inside openai_client),
so these tests run without the package installed and without any network.
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import transcribe_recording as tr  # noqa: E402


class _Obj:
    def __init__(self, text):
        self.text = text


class _Dumpable:
    def model_dump(self):
        return {"text": "dumped"}


class TranscriptTextTests(unittest.TestCase):
    def test_reads_attribute(self):
        self.assertEqual(tr.transcript_text(_Obj("hello")), "hello")

    def test_reads_dict(self):
        self.assertEqual(tr.transcript_text({"text": "hi"}), "hi")

    def test_falls_back_to_str(self):
        self.assertEqual(tr.transcript_text(123), "123")


class ResultJsonableTests(unittest.TestCase):
    def test_uses_model_dump(self):
        self.assertEqual(tr.result_jsonable(_Dumpable()), {"text": "dumped"})

    def test_passes_through_dict(self):
        self.assertEqual(tr.result_jsonable({"a": 1}), {"a": 1})

    def test_wraps_scalar(self):
        self.assertEqual(tr.result_jsonable("plain"), {"text": "plain"})


class OutputPathsTests(unittest.TestCase):
    def test_has_expected_keys(self):
        paths = tr.output_paths(Path("/tmp/job"))
        for key in ("txt", "md", "json", "summary", "manifest", "progress"):
            self.assertIn(key, paths)


class EnsureJobTests(unittest.TestCase):
    def test_recording_in_own_folder_is_left_in_place(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_root = Path(tmp) / "output"
            job_dir = output_root / "20260101-meet"
            job_dir.mkdir(parents=True)
            recording = job_dir / "recording.mp4"
            recording.write_text("x")
            resolved_dir, resolved_rec = tr.ensure_job(recording, output_root)
            self.assertEqual(resolved_dir, job_dir.resolve())
            self.assertEqual(resolved_rec, recording.resolve())


class BuildSummaryPromptTests(unittest.TestCase):
    def test_owner_appears_when_set(self):
        prompt = tr.build_summary_prompt("transcript", owner="Alex Doe", aliases=["AD"])
        self.assertIn("Alex Doe", prompt)
        self.assertIn("AD", prompt)
        self.assertIn("ACTION ITEMS", prompt)

    def test_neutral_when_owner_empty(self):
        prompt = tr.build_summary_prompt("transcript", owner="")
        self.assertNotIn("meeting owner is", prompt)
        self.assertIn("preparing meeting notes.", prompt)

    def test_truncates_transcript_to_max_chars(self):
        long_text = "x" * 500
        prompt = tr.build_summary_prompt(long_text, owner="", max_chars=100)
        self.assertNotIn("x" * 101, prompt)


if __name__ == "__main__":
    unittest.main()
