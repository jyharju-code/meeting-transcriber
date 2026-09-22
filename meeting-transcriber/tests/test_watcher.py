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

            def next_detection(_config):
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


if __name__ == "__main__":
    unittest.main()
