"""Unit tests for the meeting watcher's pure logic.

These avoid AppleScript, subprocesses, and the network by exercising the
regexes, detection dispatch (with patched I/O), and command-building helpers.
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import meeting_transcriber as mt  # noqa: E402


class MeetRegexTests(unittest.TestCase):
    def test_matches_active_meet_room(self):
        self.assertIsNotNone(mt.MEET_RE.search("https://meet.google.com/abc-defg-hij"))

    def test_ignores_landing_and_marketing_urls(self):
        self.assertIsNone(mt.MEET_RE.search("https://meet.google.com/"))
        self.assertIsNone(mt.MEET_RE.search("https://meet.google.com/about"))


class TeamsRegexTests(unittest.TestCase):
    def test_matches_teams_hosts(self):
        self.assertIsNotNone(mt.TEAMS_URL_RE.search("https://teams.microsoft.com/l/meetup-join/x"))
        self.assertIsNotNone(mt.TEAMS_URL_RE.search("https://teams.live.com/meet/123"))

    def test_ignores_other_hosts(self):
        self.assertIsNone(mt.TEAMS_URL_RE.search("https://example.com/teams"))


class DetectMeetingTests(unittest.TestCase):
    def test_detects_google_meet_tab(self):
        with mock.patch.object(mt, "browser_tabs", return_value=[("https://meet.google.com/abc-defg-hij", "Meet")]), \
             mock.patch.object(mt, "teams_window_titles", return_value=[]):
            detection = mt.detect_meeting({"browser_apps": ["Google Chrome"]})
        self.assertIsNotNone(detection)
        self.assertEqual(detection.provider, "Google Meet")

    def test_detects_teams_desktop_window(self):
        with mock.patch.object(mt, "browser_tabs", return_value=[]), \
             mock.patch.object(mt, "teams_window_titles", return_value=["Weekly sync | Microsoft Teams meeting"]):
            detection = mt.detect_meeting({"browser_apps": []})
        self.assertIsNotNone(detection)
        self.assertEqual(detection.provider, "Microsoft Teams")

    def test_returns_none_when_no_meeting(self):
        with mock.patch.object(mt, "browser_tabs", return_value=[("https://news.example.com", "News")]), \
             mock.patch.object(mt, "teams_window_titles", return_value=["Inbox"]):
            self.assertIsNone(mt.detect_meeting({"browser_apps": ["Google Chrome"]}))

    def test_ignores_teams_prejoin_page(self):
        tab = ("https://teams.microsoft.com/meet/123", "Liity keskusteluun | Microsoft Teams")
        with mock.patch.object(mt, "browser_tabs", return_value=[tab]), \
             mock.patch.object(mt, "teams_window_titles", return_value=[]):
            self.assertIsNone(mt.detect_meeting({"browser_apps": ["Google Chrome"]}))


class WithPlaceholdersTests(unittest.TestCase):
    def test_substitutes_output_and_status(self):
        cmd = ["rec", "--output", "{output}", "--status-file", "{status}"]
        result = mt.with_placeholders(cmd, Path("/tmp/a.mp4"), Path("/tmp/s.json"))
        self.assertEqual(result, ["rec", "--output", "/tmp/a.mp4", "--status-file", "/tmp/s.json"])

    def test_drops_dangling_status_flag_when_unset(self):
        cmd = ["rec", "--output", "{output}", "--status-file", "{status}", "--max", "10"]
        result = mt.with_placeholders(cmd, Path("/tmp/a.mp4"), None)
        # The orphaned --status-file flag must not survive without its value.
        self.assertEqual(result, ["rec", "--output", "/tmp/a.mp4", "--max", "10"])


class HasJobArtifactsTests(unittest.TestCase):
    def test_false_for_empty_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(mt.has_job_artifacts(Path(tmp)))

    def test_true_when_recording_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "recording.mp4").write_text("x")
            self.assertTrue(mt.has_job_artifacts(Path(tmp)))


class DashboardControlTests(unittest.TestCase):
    def make_session(self, root: Path, command_id: str = "job-1") -> mt.ActiveRecording:
        command_file = root / "command.json"
        recorder = mt.DashboardCommandProcess({}, command_file, command_id, root / "recording.mp4")
        return mt.ActiveRecording(
            recorder=recorder,
            audio_path=root / "recording.mp4",
            started_at=123.0,
            detection=mt.Detection("Microsoft Teams", "Chrome", "Meeting"),
        )

    def test_matching_stopped_ack_clears_every_session_handle(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = self.make_session(Path(tmp))
            self.assertTrue(mt.acknowledgement_finishes_session(session, {"commandID": "job-1", "state": "stopped"}))
            self.assertIsNone(session.recorder)
            self.assertIsNone(session.audio_path)
            self.assertIsNone(session.started_at)
            self.assertIsNone(session.detection)

    def test_matching_failed_ack_also_clears_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = self.make_session(Path(tmp))
            self.assertTrue(mt.acknowledgement_finishes_session(session, {"commandID": "job-1", "state": "failed"}))
            self.assertIsNone(session.recorder)

    def test_wrong_ack_does_not_clear_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = self.make_session(Path(tmp))
            self.assertFalse(mt.acknowledgement_finishes_session(session, {"commandID": "old-job", "state": "stopped"}))
            self.assertIsNotNone(session.recorder)

    def test_malformed_suppression_file_fails_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            suppression = Path(tmp) / "suppression.json"
            suppression.write_text("not json", encoding="utf-8")
            config = {"auto_suppression_file": str(suppression), "log_file": str(Path(tmp) / "test.log")}
            self.assertTrue(mt.read_auto_suppression(config)["suppressed"])

    def test_clear_suppression_removes_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            suppression = Path(tmp) / "suppression.json"
            suppression.write_text('{"suppressed": true}', encoding="utf-8")
            config = {"auto_suppression_file": str(suppression), "log_file": str(Path(tmp) / "test.log")}
            mt.clear_auto_suppression(config)
            self.assertFalse(suppression.exists())

    def test_watch_waits_for_suppression_misses_before_later_start(self):
        class EndWatch(Exception):
            pass

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            suppression = root / "suppression.json"
            suppression.write_text('{"suppressed": true}', encoding="utf-8")
            config_path = root / "config.json"
            config_path.write_text(
                '{'
                '"poll_seconds": 0,'
                '"start_after_consecutive_detections": 1,'
                '"stop_after_consecutive_misses": 2,'
                f'"auto_suppression_file": "{suppression}",'
                f'"dashboard_ack_file": "{root / "ack.json"}",'
                f'"output_dir": "{root / "output"}",'
                f'"log_file": "{root / "test.log"}"'
                '}',
                encoding="utf-8",
            )
            meeting = mt.Detection("Microsoft Teams", "Chrome", "Meeting")
            recorder = mock.Mock()
            recorder.poll.return_value = None
            detections = iter([meeting, None, None, meeting])

            def next_detection(_config, _sticky=None):
                try:
                    return next(detections)
                except StopIteration as exc:
                    raise EndWatch from exc

            with mock.patch.object(mt, "detect_meeting", side_effect=next_detection), \
                 mock.patch.object(mt, "start_recording", return_value=(recorder, root / "recording.mp4")) as start, \
                 mock.patch.object(mt.time, "sleep"):
                with self.assertRaises(EndWatch):
                    mt.watch(config_path)

            start.assert_called_once_with(mock.ANY, meeting)
            self.assertFalse(suppression.exists())


SUBJECT = "Juhana Harju x Viran: julkiset hankinnat ja tekoäly"
COMPACT = f"Meeting compact view | {SUBJECT} | Personal | jyharju@gmail.com | Microsoft Teams"
JOIN = f"Meeting join | {SUBJECT} | Personal | jyharju@gmail.com | Microsoft Teams"
STAGE = f"{SUBJECT} | Personal | jyharju@gmail.com | Microsoft Teams"


class TeamsSubjectTests(unittest.TestCase):
    """Real window titles from the 30.9.2026 meeting that got split in six parts."""

    def test_subject_from_compact_view_and_join_screen(self):
        self.assertEqual(mt.teams_meeting_subject(COMPACT), SUBJECT)
        self.assertEqual(mt.teams_meeting_subject(JOIN), SUBJECT)

    def test_generic_subject_is_not_learned(self):
        self.assertEqual(mt.teams_meeting_subject("Meeting join | Microsoft Teams meeting | anonymous | Microsoft Teams"), "")

    def test_nav_windows_are_recognised(self):
        self.assertTrue(mt.teams_title_is_nav(f"Chat | {SUBJECT} | Personal | Microsoft Teams"))
        self.assertFalse(mt.teams_title_is_nav(STAGE))

    def test_meeting_window_without_the_word_meeting_needs_the_subject(self):
        with mock.patch.object(mt, "browser_tabs", return_value=[]), \
             mock.patch.object(mt, "teams_window_titles", return_value=[STAGE]):
            self.assertIsNone(mt.detect_meeting({"browser_apps": []}))
            detection = mt.detect_meeting({"browser_apps": []}, [SUBJECT])
        self.assertIsNotNone(detection)
        self.assertEqual(detection.source, mt.STICKY_SOURCE)
        self.assertEqual(detection.subject, SUBJECT)

    def test_meeting_chat_after_the_call_does_not_count(self):
        with mock.patch.object(mt, "browser_tabs", return_value=[]), \
             mock.patch.object(mt, "teams_window_titles", return_value=[f"Chat | {SUBJECT} | Personal | Microsoft Teams"]):
            self.assertIsNone(mt.detect_meeting({"browser_apps": []}, [SUBJECT]))

    def test_compact_view_detection_carries_subject(self):
        with mock.patch.object(mt, "browser_tabs", return_value=[]), \
             mock.patch.object(mt, "teams_window_titles", return_value=[COMPACT]):
            detection = mt.detect_meeting({"browser_apps": []})
        self.assertEqual(detection.subject, SUBJECT)


class WatchContinuityTests(unittest.TestCase):
    class EndWatch(Exception):
        pass

    def run_watch(self, sequence, extra_config=""):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.json"
            config_path.write_text(
                '{"poll_seconds": 0, "start_after_consecutive_detections": 2,'
                '"stop_after_consecutive_misses": 4,'
                '"stop_after_consecutive_misses_while_recording": 5,'
                f'"auto_suppression_file": "{root / "s.json"}",'
                f'"dashboard_ack_file": "{root / "ack.json"}",'
                f'"output_dir": "{root / "out"}",'
                f'"log_file": "{root / "test.log"}"{extra_config}' '}',
                encoding="utf-8",
            )
            items = iter(sequence)
            seen_sticky = []

            def next_detection(_config, sticky=None):
                seen_sticky.append(list(sticky or []))
                try:
                    return next(items)
                except StopIteration as exc:
                    raise self.EndWatch from exc

            recorder = mock.Mock()
            recorder.poll.return_value = None
            with mock.patch.object(mt, "detect_meeting", side_effect=next_detection), \
                 mock.patch.object(mt, "teams_window_titles", return_value=[]), \
                 mock.patch.object(mt, "start_recording", return_value=(recorder, root / "r.mp4")) as start, \
                 mock.patch.object(mt, "stop_recording") as stop, \
                 mock.patch.object(mt.time, "sleep"):
                with self.assertRaises(self.EndWatch):
                    mt.watch(config_path)
            return start, stop, seen_sticky

    def test_short_focus_changes_do_not_split_the_recording(self):
        compact = mt.Detection("Microsoft Teams", "Teams app", COMPACT, SUBJECT)
        # 4 misses (like the 40 s drop-outs that split the 30.9 meeting) < 5 allowed.
        seq = [compact, compact, None, None, None, None, compact, None, None, None, None, compact]
        start, stop, sticky = self.run_watch(seq)
        start.assert_called_once()
        stop.assert_not_called()
        self.assertIn(SUBJECT, sticky[-1])  # subject is offered for the meeting window

    def test_recording_stops_after_the_long_grace(self):
        compact = mt.Detection("Microsoft Teams", "Teams app", COMPACT, SUBJECT)
        start, stop, _ = self.run_watch([compact, compact] + [None] * 5 + [None])
        start.assert_called_once()
        stop.assert_called_once()

    def test_switch_to_another_meeting_closes_the_first(self):
        first = mt.Detection("Microsoft Teams", "Teams app", COMPACT, SUBJECT)
        other = mt.Detection("Microsoft Teams", "Teams app", "Meeting compact view | Toinen kokous | Microsoft Teams", "Toinen kokous")
        start, stop, _ = self.run_watch([first, first, other])
        stop.assert_called_once()

    def test_join_screen_subject_bridges_to_the_meeting_window(self):
        join = mt.Detection("Microsoft Teams", "Teams app", JOIN, SUBJECT)
        stage = mt.Detection("Microsoft Teams", mt.STICKY_SOURCE, STAGE, SUBJECT)
        # User clicks Join after one poll; the stage window only matches by subject.
        start, _stop, sticky = self.run_watch([join, stage, stage])
        start.assert_called_once()
        self.assertIn(SUBJECT, sticky[1])


class CatchupTests(unittest.TestCase):
    def make_job(self, root: Path, name: str, age_s: float, now: float, extra=()) -> Path:
        import os
        job = root / name
        job.mkdir(parents=True)
        rec = job / "recording.mp4"
        rec.write_bytes(b"x" * 10)
        os.utime(rec, (now - age_s, now - age_s))
        for fname in extra:
            (job / fname).write_text("{}", encoding="utf-8")
        return job

    def test_untranscribed_recording_is_picked_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            now = 1_000_000.0
            job = self.make_job(Path(tmp), "20260911-153852-manual", 3600, now)
            self.assertEqual(mt.job_needs_transcription(job, now, 600, 48 * 3600), job / "recording.mp4")

    def test_attempted_or_done_jobs_are_left_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            now = 1_000_000.0
            attempted = self.make_job(Path(tmp), "a", 3600, now, ["progress.json"])
            done = self.make_job(Path(tmp), "b", 3600, now, ["transcript.txt"])
            self.assertIsNone(mt.job_needs_transcription(attempted, now, 600, 48 * 3600))
            self.assertIsNone(mt.job_needs_transcription(done, now, 600, 48 * 3600))

    def test_fresh_and_old_recordings_are_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            now = 1_000_000.0
            fresh = self.make_job(Path(tmp), "fresh", 60, now)        # may still be recording
            old = self.make_job(Path(tmp), "old", 30 * 86400, now)    # old backlog: user decides
            self.assertIsNone(mt.job_needs_transcription(fresh, now, 600, 48 * 3600))
            self.assertIsNone(mt.job_needs_transcription(old, now, 600, 48 * 3600))

    def test_find_catchup_job_prefers_newest(self):
        with tempfile.TemporaryDirectory() as tmp:
            import time as _time
            now = _time.time()
            root = Path(tmp)
            self.make_job(root, "20260929-100000-manual", 7200, now)
            newer = self.make_job(root, "20260930-100000-manual", 3600, now)
            found = mt.find_catchup_job({"output_dir": str(root)}, now)
            self.assertEqual(found, newer / "recording.mp4")

    def test_failed_attempt_is_retried_later_but_not_forever(self):
        import os
        with tempfile.TemporaryDirectory() as tmp:
            now = 1_000_000.0
            job = self.make_job(Path(tmp), "20261001-080000-microsoft-teams", 3 * 3600, now)
            progress = job / "progress.json"
            progress.write_text('{"stage": "error"}', encoding="utf-8")
            os.utime(progress, (now - 600, now - 600))
            self.assertIsNone(mt.job_needs_transcription(job, now, 600, 48 * 3600, 3600, 6))  # too soon
            os.utime(progress, (now - 7200, now - 7200))
            self.assertIsNotNone(mt.job_needs_transcription(job, now, 600, 48 * 3600, 3600, 6))  # retry
            (job / mt.CATCHUP_ATTEMPTS_FILE).write_text("6")
            self.assertIsNone(mt.job_needs_transcription(job, now, 600, 48 * 3600, 3600, 6))  # gave up

    def test_done_job_is_never_retried(self):
        import os
        with tempfile.TemporaryDirectory() as tmp:
            now = 1_000_000.0
            job = self.make_job(Path(tmp), "j", 3 * 3600, now)
            progress = job / "progress.json"
            progress.write_text('{"stage": "skipped"}', encoding="utf-8")
            os.utime(progress, (now - 7200, now - 7200))
            self.assertIsNone(mt.job_needs_transcription(job, now, 600, 48 * 3600))

    def test_stale_recording_status_does_not_block(self):
        import os
        with tempfile.TemporaryDirectory() as tmp:
            status = Path(tmp) / "status.json"
            status.write_text('{"recording": true}', encoding="utf-8")
            config = {"status_file": str(status)}
            now = status.stat().st_mtime
            self.assertTrue(mt.dashboard_is_recording(config, now + 5))
            self.assertFalse(mt.dashboard_is_recording(config, now + 3600))  # crashed app left it true


if __name__ == "__main__":
    unittest.main()
