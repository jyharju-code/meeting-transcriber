"""Unit tests for the meeting watcher's pure logic.

These avoid AppleScript, subprocesses, and the network by exercising the
regexes, detection dispatch (with patched I/O), and command-building helpers.
"""

import sys
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
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(mt.has_job_artifacts(Path(tmp)))

    def test_true_when_recording_present(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "recording.mp4").write_text("x")
            self.assertTrue(mt.has_job_artifacts(Path(tmp)))


if __name__ == "__main__":
    unittest.main()
