"""Soniox + MAI (D8, D9): recognizers, free/paid Azure order, windows and the fallback chain.

No network: every outbound call is mocked.
"""

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dual_mode  # noqa: E402
import policy  # noqa: E402
import providers  # noqa: E402
import transcribe_recording as tr  # noqa: E402


def tok(text, speaker, start):
    return {"text": text, "speaker": speaker, "start_ms": start, "end_ms": start + 100}


class SonioxTests(unittest.TestCase):
    def test_tokens_become_speaker_turns(self):
        turns = providers.turns_from_soniox([
            tok("Moi", "1", 0), tok(".", "1", 100), tok(" Hei", "2", 900), tok(" vaan", "2", 1000),
            {"text": " hello", "speaker": "2", "start_ms": 1100, "end_ms": 1200, "translation_status": "translation"},
            tok(" Joo", "1", 2000)])
        self.assertEqual([(t["speaker"], t["text"]) for t in turns], [("S1", "Moi."), ("S2", "Hei vaan"), ("S1", "Joo")])
        self.assertEqual(turns[1]["start"], 0.9)

    def test_upload_and_transcription_are_deleted_even_on_failure(self):
        calls = []

        def fake(method, url, headers, body=None, **kw):
            calls.append((method, url))
            if url.endswith("/v1/files") and method == "POST":
                return b'{"id": "f1"}'
            if url.endswith("/v1/transcriptions") and method == "POST":
                return b'{"id": "t1"}'
            if method == "GET":
                return b'{"status": "error", "error_message": "bad audio"}'
            return b"{}"

        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / "a.mp3"
            audio.write_bytes(b"x")
            s = providers.SonioxTranscriber(poll_seconds=0)
            with mock.patch.dict(os.environ, {"SONIOX_API_KEY": "k"}), mock.patch.object(providers, "api_http", side_effect=fake):
                with self.assertRaises(providers.ProviderError):
                    s.transcribe_file(audio)
        deletes = [u for m, u in calls if m == "DELETE"]
        self.assertTrue(any(u.endswith("/v1/transcriptions/t1") for u in deletes))
        self.assertTrue(any(u.endswith("/v1/files/f1") for u in deletes))


class AzureMaiTests(unittest.TestCase):
    def make(self, tmp, cap=50.0):
        return providers.AzureMaiTranscriber(endpoints=[
            {"name": "f0", "url": "https://free.cognitiveservices.azure.com", "key_env": "AZ_F0", "paid": False},
            {"name": "s0", "url": "https://paid.cognitiveservices.azure.com", "key_env": "AZ_S0", "paid": True}],
            usage_file=Path(tmp) / "kaytto.json", paid_hours_per_month=cap)

    OK = json.dumps({"durationMilliseconds": 3600000, "phrases": [
        {"speaker": 1, "offsetMilliseconds": 0, "durationMilliseconds": 900, "text": "Hei."},
        {"speaker": 2, "offsetMilliseconds": 1000, "durationMilliseconds": 900, "text": "Moi."}]}).encode()

    def test_free_tier_first_then_paid_when_quota_used(self):
        used = []

        def fake(method, url, headers, body=None, **kw):
            used.append(url.split("//")[1].split(".")[0])
            if "free" in url:
                raise providers.ProviderError("HTTP 403: Out of call volume quota for F0 pricing tier")
            return self.OK

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"AZ_F0": "a", "AZ_S0": "b"}), \
                mock.patch.object(providers, "api_http", side_effect=fake):
            m = self.make(tmp)
            audio = Path(tmp) / "a.mp3"
            audio.write_bytes(b"x")
            data = m.transcribe_file(audio, 3600)
            self.assertEqual((data["endpoint"], data["paid"]), ("s0", True))
            self.assertEqual([t["speaker"] for t in data["turns"]], ["S1", "S2"])
            m.transcribe_file(audio, 3600)  # the exhausted free resource is skipped for the rest of the month
        self.assertEqual(used, ["free", "paid", "paid"])

    def test_paid_hours_cap_stops_mai(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"AZ_S0": "b"}, clear=True), \
                mock.patch.object(providers, "api_http", return_value=self.OK) as http:
            m = self.make(tmp, cap=1.5)
            audio = Path(tmp) / "a.mp3"
            audio.write_bytes(b"x")
            m.transcribe_file(audio, 3600)  # 1 h of 1.5 h
            with self.assertRaises(providers.ProviderError):
                m.transcribe_file(audio, 3600)  # would exceed the cap
            used = json.loads((Path(tmp) / "kaytto.json").read_text())[time.strftime("%Y-%m")]["s0_s"]
        self.assertEqual(http.call_count, 1)
        self.assertEqual(used, 3600.0)


class WindowTests(unittest.TestCase):
    def test_windows_cut_at_turn_starts_and_cover_recording(self):
        turns = [{"start": s, "end": s + 50, "text": "x", "speaker": "S1"} for s in range(0, 1900, 100)]
        wins = dual_mode.windows(turns, 1950, size=600)
        self.assertEqual([w[0] for w in wins], [0.0, 600, 1200, 1800])
        self.assertGreater(wins[-1][1], 1950)
        inside = dual_mode.in_window(turns, wins[1])
        self.assertEqual(inside[0]["start"], 0)


class CallLogTests(unittest.TestCase):
    def test_new_hosts_are_described_and_named(self):
        d = policy.describe_request("https://api.soniox.com/v1/transcriptions/t1")
        self.assertEqual((d["host"], d["kind"]), ("api.soniox.com", "soniox-transcription"))
        d = policy.describe_request("https://mt.cognitiveservices.azure.com/speechtotext/transcriptions:transcribe?api-version=x")
        self.assertEqual(d["model"], "MAI-Transcribe-2")
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp)
            policy.set_call_log(job)
            policy.record_call("https://api.soniox.com/v1/files", None, "ok")
            policy.record_call("https://mt.cognitiveservices.azure.com/x", None, "ok")
            policy.record_call("https://aiplatform.eu.rep.googleapis.com/v1/x", None, "ok")
            policy.record_call("https://generativelanguage.googleapis.com/v1/x", None, "HTTP 500")
            policy.set_call_log(None)
            self.assertEqual(policy.service_names(job), ["Soniox", "Microsoft Azure (MAI)", "Google EU"])


class FallbackTests(unittest.TestCase):
    def test_perustaso_uses_gemini_when_soniox_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp)
            rec = job / "recording.mp4"
            rec.write_bytes(b"x")
            args = mock.Mock(summary="off", chunk_seconds=None)
            cfg = {"transcribe_output_format": "md"}
            with mock.patch.object(tr, "soniox_perustaso", return_value=None) as son, \
                    mock.patch.object(tr, "build_transcriber", return_value=None) as gem:
                rc = tr.run_perustaso(cfg, args, rec, job, job / "progress.json",
                                      {"sijainti": "maailmanlaajuinen", "laatu": "perus"})
            son.assert_called_once()
            gem.assert_called_once()  # the old Gemini path was tried
            self.assertEqual(rc, 1)

    def test_perustaso_soniox_writes_outputs_and_queues_upgrade(self):
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp)
            rec = job / "recording.mp4"
            rec.write_bytes(b"x")
            args = mock.Mock(summary="off", chunk_seconds=None)
            with mock.patch.object(tr, "soniox_perustaso", return_value="[00:00:01] Puhuja 1: Hei."), \
                    mock.patch.object(tr, "build_transcriber") as gem, mock.patch.object(policy, "notify"):
                rc = tr.run_perustaso({"transcribe_output_format": "md"}, args, rec, job, job / "progress.json",
                                      {"sijainti": "maailmanlaajuinen", "laatu": "huippu"})
            gem.assert_not_called()
            self.assertEqual(rc, 0)
            self.assertIn("Puhuja 1: Hei.", (job / "transcript.md").read_text())
            self.assertTrue((job / tr.UPGRADE_QUEUE).exists())

    def test_eu_position_never_uses_soniox(self):
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp)
            rec = job / "recording.mp4"
            rec.write_bytes(b"x")
            args = mock.Mock(summary="off", chunk_seconds=None)
            with mock.patch.object(tr, "soniox_perustaso") as son, \
                    mock.patch.object(tr, "build_transcriber", return_value=None):
                tr.run_perustaso({"transcribe_output_format": "md"}, args, rec, job, job / "progress.json",
                                 {"sijainti": "eu", "laatu": "perus"})
            son.assert_not_called()

    def test_huipputaso_falls_back_to_gemini_max_when_both_ears_fail(self):
        import max_mode
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp)
            rec = job / "recording.mp4"
            rec.write_bytes(b"x")
            policy.lock_job(job, {"sijainti": "maailmanlaajuinen", "laatu": "huippu"}, "litterointi")
            args = mock.Mock(summary="off")
            with mock.patch.object(dual_mode, "run", side_effect=providers.ProviderError("ei")), \
                    mock.patch.object(max_mode, "run", return_value="teksti") as old, \
                    mock.patch.object(tr, "summarize_safely", return_value=(True, "x")), \
                    mock.patch.object(policy, "notify") as note:
                rc = tr.run_huipputaso({}, args, rec, job)
            old.assert_called_once()
            self.assertEqual(rc, 0)
            self.assertTrue(any("heikennetty" in c.args[0].lower() for c in note.call_args_list))

    def test_dual_route_degrades_to_soniox_plus_gemini(self):
        son = {"turns": [{"speaker": "S1", "start": 1.0, "end": 2.0, "text": "Hei."}]}
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp)
            rec = job / "recording.mp4"
            rec.write_bytes(b"x")
            judged = {"lines": [{"time": "00:00:01", "speaker": "S1", "text": "Hei."}], "method": "adjudicated"}
            with mock.patch.object(dual_mode, "cached_run", side_effect=lambda j, name, *a: son if name == dual_mode.SONIOX_CACHE else None), \
                    mock.patch.object(dual_mode.P, "recording_mp3", return_value=job / "a.mp3"), \
                    mock.patch.object(dual_mode.P, "text_generator", return_value=lambda *a, **k: "{}"), \
                    mock.patch.object(dual_mode.P, "ap_settings", return_value=("p", "eu", "m")), \
                    mock.patch.object(dual_mode.subprocess, "run"), \
                    mock.patch.object(tr, "recording_duration_seconds", return_value=10.0), \
                    mock.patch.object(dual_mode.MM, "adjudicate", return_value=judged), \
                    mock.patch.object(dual_mode.MM, "unify_speakers", return_value={}), \
                    mock.patch.object(dual_mode.T, "clean_segment", side_effect=lambda k, m, lines: {"lines": lines}), \
                    mock.patch.object(dual_mode.P.GeminiTranscriber, "available", return_value=False), \
                    mock.patch.object(Path, "read_bytes", return_value=b"x"), \
                    mock.patch.object(policy, "notify") as note:
                dual_mode.run({}, rec, job, None)
            meta = json.loads((job / "huipputaso.json").read_text())
            self.assertEqual(meta["reitti"], "Soniox + Gemini 3.5 Transcribe")
            self.assertTrue(meta["heikennetty"])
            self.assertIn("Heikennetty", (job / "transcript.md").read_text())
            note.assert_called_once()


if __name__ == "__main__":
    unittest.main()
