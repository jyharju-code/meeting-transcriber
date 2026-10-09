"""Tests for the switches, their lock into a meeting, the call log and the red lamp (D1-D5).

No network: every outbound call is mocked.
"""

import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import meeting_transcriber as mt  # noqa: E402
import policy  # noqa: E402
import providers  # noqa: E402
import transcribe_recording as tr  # noqa: E402


class SwitchTests(unittest.TestCase):
    def test_defaults_are_global_and_huipputaso(self):
        # D10: Huipputaso is on by default.
        self.assertEqual(policy.normalize({}), {"sijainti": "maailmanlaajuinen", "laatu": "huippu"})
        self.assertEqual(policy.normalize({"sijainti": "mars", "laatu": "?"}),
                         {"sijainti": "maailmanlaajuinen", "laatu": "huippu"})
        self.assertEqual(policy.normalize({"laatu": "perus"})["laatu"], "perus")

    def test_read_write_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = {"switch_file": str(Path(tmp) / "kytkimet.json")}
            self.assertEqual(policy.read_switches(cfg)["sijainti"], "maailmanlaajuinen")
            policy.write_switches({"sijainti": "eu", "laatu": "huippu"}, cfg)
            self.assertEqual(policy.read_switches(cfg), {"sijainti": "eu", "laatu": "huippu"})

    def test_lock_stricter_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp)
            policy.lock_job(job, {"sijainti": "eu", "laatu": "perus"}, "nauhoitus")
            # switched back to global before transcription: the meeting stays EU
            locked = policy.lock_job(job, {"sijainti": "maailmanlaajuinen", "laatu": "huippu"}, "litterointi")
            self.assertEqual(locked["sijainti"], "eu")
            self.assertEqual(locked["laatu"], "huippu")
            self.assertEqual([h["hetki"] for h in locked["historia"]], ["nauhoitus", "litterointi"])

    def test_late_switch_to_eu_still_applies(self):
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp)
            policy.lock_job(job, {"sijainti": "maailmanlaajuinen"}, "nauhoitus")
            self.assertEqual(policy.lock_job(job, {"sijainti": "eu"}, "litterointi")["sijainti"], "eu")

    def test_title_has_no_effect(self):
        # D1/D2: the meeting title never changes location or level.
        self.assertFalse(hasattr(tr, "resolve_preset"))
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp)
            (job / "meeting.json").write_text(json.dumps({"subject": "EU-haastattelu: luottamuksellinen"}), encoding="utf-8")
            locked = policy.lock_job(job, {"sijainti": "maailmanlaajuinen", "laatu": "perus"}, "litterointi")
            self.assertEqual((locked["sijainti"], locked["laatu"]), ("maailmanlaajuinen", "perus"))


class CallLogTests(unittest.TestCase):
    def test_key_is_never_logged(self):
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp)
            policy.set_call_log(job)
            try:
                policy.record_call("https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash"
                                   ":generateContent?key=SALAINEN123", None, "ok")
            finally:
                policy.set_call_log(None)
            text = (job / policy.CALL_LOG).read_text(encoding="utf-8")
            self.assertNotIn("SALAINEN123", text)
            entry = json.loads(text)
            self.assertEqual(entry["host"], "generativelanguage.googleapis.com")
            self.assertEqual(entry["model"], "gemini-3.5-flash")
            self.assertEqual(entry["kind"], "generateContent")

    def test_interactions_model_from_body(self):
        d = policy.describe_request("https://generativelanguage.googleapis.com/v1beta/interactions?key=x",
                                    json.dumps({"model": "gemini-3.5-transcribe"}).encode())
        self.assertEqual((d["kind"], d["model"]), ("interactions", "gemini-3.5-transcribe"))

    def test_gemini_http_records_every_attempt(self):
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp)
            policy.set_call_log(job)
            req = providers.urllib.request.Request(
                "https://aiplatform.eu.rep.googleapis.com/v1/projects/p/locations/eu/publishers/google/models/"
                "gemini-3.5-flash:generateContent", data=b"{}")
            resp = mock.MagicMock()
            resp.__enter__.return_value.read.return_value = b'{"ok": 1}'
            try:
                with mock.patch.object(providers.urllib.request, "urlopen", return_value=resp):
                    providers.gemini_http(req, timeout=5)
            finally:
                policy.set_call_log(None)
            calls = policy.calls(job)
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0]["host"], "aiplatform.eu.rep.googleapis.com")
            self.assertEqual(policy.outside_eu(job), [])


class LampTests(unittest.TestCase):
    def make_job(self, root: Path, sijainti: str, hosts: list) -> Path:
        job = root / "20261003-100000-manual"
        job.mkdir()
        policy.lock_job(job, {"sijainti": sijainti}, "nauhoitus")
        with (job / policy.CALL_LOG).open("w", encoding="utf-8") as f:
            for h in hosts:
                f.write(json.dumps({"host": h, "tulos": "ok"}) + "\n")
        return job

    def test_eu_job_inside_eu_is_green(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = {"lamp_file": str(Path(tmp) / "lamppu.json")}
            job = self.make_job(Path(tmp), "eu", ["aiplatform.eu.rep.googleapis.com"])
            with mock.patch.object(policy, "notify") as notify:
                self.assertFalse(policy.report_violation(job, cfg))
            notify.assert_not_called()
            self.assertEqual(policy.status_line(job), "")  # D13: no routine route in the outputs

    def test_eu_job_that_went_outside_lights_the_lamp(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = {"lamp_file": str(Path(tmp) / "lamppu.json")}
            job = self.make_job(Path(tmp), "eu", ["aiplatform.eu.rep.googleapis.com",
                                                   "generativelanguage.googleapis.com"])
            with mock.patch.object(policy, "notify") as notify:
                self.assertTrue(policy.report_violation(job, cfg))
                self.assertTrue(policy.report_violation(job, cfg))  # reported once only
            notify.assert_called_once()
            lamp = policy.read_lamp(cfg)
            self.assertEqual([r["palaveri"] for r in lamp["rikkeet"]], [job.name])
            self.assertIn("🔴", policy.status_line(job))

    def test_global_job_never_lights_the_lamp(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = {"lamp_file": str(Path(tmp) / "lamppu.json")}
            job = self.make_job(Path(tmp), "maailmanlaajuinen", ["generativelanguage.googleapis.com"])
            self.assertFalse(policy.report_violation(job, cfg))
            self.assertEqual(policy.status_line(job), "")

    def test_stamp_only_marks_the_exception_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            job = self.make_job(Path(tmp), "eu", ["aiplatform.eu.rep.googleapis.com"])
            old = "# Kokousmuistiinpano\n\n> 🇪🇺 **Käsitelty: EU** (vain EU-palvelu). Huipputaso.\n\nTeksti.\n"
            (job / "summary.md").write_text(old, encoding="utf-8")
            policy.stamp_outputs(job)  # an old routine stamp is removed
            self.assertNotIn("Käsitelty", (job / "summary.md").read_text(encoding="utf-8"))
            with (job / policy.CALL_LOG).open("a", encoding="utf-8") as f:
                f.write(json.dumps({"host": "generativelanguage.googleapis.com", "tulos": "ok"}) + "\n")
            policy.stamp_outputs(job)
            policy.stamp_outputs(job)
            text = (job / "summary.md").read_text(encoding="utf-8")
            self.assertEqual(text.count("🔴"), 1)
            self.assertTrue(text.startswith("# Kokousmuistiinpano\n\n> 🔴"))

class RoutingTests(unittest.TestCase):
    base = {"agent_platform_project": "p", "agent_platform_location": "eu"}

    def test_global_perustaso_is_transcribe_smart(self):
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "k"}, clear=True):
            t = tr.build_transcriber(dict(self.base), {"sijainti": "maailmanlaajuinen", "laatu": "perus"})
        self.assertIsInstance(t, providers.GeminiTranscriber)
        self.assertEqual((t.model, t.mode), ("gemini-3.5-transcribe", "smart"))

    def test_global_huipputaso_first_pass_is_reusable(self):
        # D5: verbatim + vocabulary, exactly Huipputaso pass T-A.
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "k"}, clear=True):
            t = tr.build_transcriber(dict(self.base), {"sijainti": "maailmanlaajuinen", "laatu": "huippu"})
        self.assertEqual(t.mode, "verbatim")

    def test_eu_uses_agent_platform_with_studio_only_as_fallback(self):
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "k"}, clear=True), \
             mock.patch.object(providers.shutil, "which", return_value="/usr/bin/gcloud"):
            t = tr.build_transcriber(dict(self.base), {"sijainti": "eu", "laatu": "perus"})
        self.assertIsInstance(t, providers.AgentPlatformTranscriber)
        self.assertEqual(t.location, "eu")
        self.assertIsInstance(t.fallback, providers.GeminiTranscriber)

    def test_eu_fallback_on_failure_is_flagged(self):
        fallback = mock.Mock()
        fallback.name = "gemini"
        fallback.available.return_value = True
        fallback.transcribe.return_value = ("hei", {"model": "gemini-3.5-transcribe"})
        t = providers.AgentPlatformTranscriber("eu", "p", fallback=fallback)
        with tempfile.TemporaryDirectory() as tmp:
            snippet = Path(tmp) / "snippet-000.m4a"
            snippet.write_bytes(b"x")
            with mock.patch.object(providers, "to_mp3", return_value=snippet), \
                 mock.patch.object(providers, "ap_generate", side_effect=providers.ProviderError("down")), \
                 mock.patch.object(providers.time, "sleep"):
                text, raw = t.transcribe(snippet)
        self.assertEqual(text, "hei")
        self.assertTrue(raw["eu_fallback"])

    def test_eu_result_is_marked_for_reuse(self):
        t = providers.AgentPlatformTranscriber("eu", "p")
        with tempfile.TemporaryDirectory() as tmp:
            snippet = Path(tmp) / "snippet-000.m4a"
            snippet.write_bytes(b"x")
            with mock.patch.object(providers, "to_mp3", return_value=snippet), \
                 mock.patch.object(providers, "ap_generate", return_value="tämä on litteraatti"):
                text, raw = t.transcribe(snippet)
        self.assertEqual(raw["prompt"], providers.FLASH_PLAIN_PROMPT_VERSION)
        self.assertEqual(raw["model"], "agent_platform:eu:gemini-3.5-flash")

    def test_text_steps_try_eu_first(self):
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "k"}, clear=True), \
             mock.patch.object(providers, "ap_generate", return_value="eu") as ap, \
             mock.patch.object(providers, "gemini_generate_text", return_value="studio") as studio:
            self.assertEqual(providers.text_generator(dict(self.base))("x"), "eu")
            ap.side_effect = providers.ProviderError("down")
            self.assertEqual(providers.text_generator(dict(self.base))("x"), "studio")
        self.assertEqual(studio.call_count, 1)


class BackgroundWorkTests(unittest.TestCase):
    def job(self, root: Path, name: str, age: float, now: float) -> Path:
        job = root / name
        job.mkdir()
        rec = job / "recording.mp4"
        rec.write_bytes(b"x")
        os.utime(rec, (now - age, now - age))
        return job

    def test_retry_schedule_grows_and_ends_after_about_a_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "m"
            marker.write_text("x")
            now = marker.stat().st_mtime
            self.assertTrue(mt.retry_due(marker, 0, now))
            self.assertFalse(mt.retry_due(marker, 1, now + 4 * 60))
            self.assertTrue(mt.retry_due(marker, 1, now + 5 * 60))
            self.assertFalse(mt.retry_due(marker, 3, now + 19 * 60))
            self.assertFalse(mt.retry_due(marker, len(mt.RETRY_MINUTES) + 1, now + 10**6))
            self.assertGreaterEqual(sum(mt.RETRY_MINUTES), 24 * 60)

    def test_summary_error_is_retried_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            now = time.time()
            job = self.job(Path(tmp), "20261003-100000-manual", 3600, now)
            (job / "transcript.txt").write_text("teksti")
            (job / "progress.json").write_text(json.dumps({"stage": "summary_error"}))
            self.assertIsNone(mt.job_needs_transcription(job, now, 600, 48 * 3600))
            self.assertIsNotNone(mt.job_needs_summary(job, now, 48 * 3600))
            work = mt.find_work({"output_dir": tmp}, now)
            self.assertEqual(work[0], "yhteenveto")

    def test_upgrade_waits_for_perustaso_and_untranscribed_go_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            now = time.time()
            root = Path(tmp)
            up = self.job(root, "20261003-090000-manual", 3600, now)
            (up / "transcript.txt").write_text("t")
            (up / mt.UPGRADE_QUEUE).write_text(json.dumps({"tila": "jonossa", "yritykset": 0}))
            (up / "progress.json").write_text(json.dumps({"stage": "transcribing"}))
            self.assertIsNone(mt.job_needs_upgrade(up, now, 48 * 3600))  # perustaso still running
            (up / "progress.json").write_text(json.dumps({"stage": "done"}))
            self.assertIsNotNone(mt.job_needs_upgrade(up, now, 48 * 3600))
            self.job(root, "20261003-080000-manual", 3600, now)  # never transcribed
            self.assertEqual(mt.find_work({"output_dir": tmp}, now)[0], "litterointi")


if __name__ == "__main__":
    unittest.main()
