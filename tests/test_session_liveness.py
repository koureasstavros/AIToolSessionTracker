import json
import tempfile
import unittest
from pathlib import Path

import session_token_viewer as viewer


class SessionLivenessTests(unittest.TestCase):
    def test_recent_session_state_without_shutdown_is_live(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory) / "session-1"
            folder.mkdir()
            (folder / "events.jsonl").write_text(
                json.dumps({"type": "assistant.message", "data": {}}) + "\n",
                encoding="utf-8",
            )
            self.assertTrue(
                viewer.detect_live_session(
                    folder,
                    "copilot-session-state",
                    now=folder.joinpath("events.jsonl").stat().st_mtime + 1,
                )
            )

    def test_shutdown_session_is_not_live(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory) / "session-1"
            folder.mkdir()
            (folder / "events.jsonl").write_text(
                json.dumps({"type": "session.shutdown", "data": {}}) + "\n",
                encoding="utf-8",
            )
            self.assertFalse(
                viewer.detect_live_session(
                    folder,
                    "copilot-session-state",
                    now=folder.joinpath("events.jsonl").stat().st_mtime + 1,
                )
            )

    def test_stale_external_source_is_not_live(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.jsonl"
            path.write_text("{}\n", encoding="utf-8")
            updated = path.stat().st_mtime
            self.assertFalse(
                viewer.detect_live_session(
                    path,
                    "external",
                    now=updated + viewer.LIVE_SESSION_WINDOW_SECONDS + 1,
                )
            )

    def test_session_at_one_minute_of_activity_is_live(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.jsonl"
            path.write_text("{}\n", encoding="utf-8")
            updated = path.stat().st_mtime
            self.assertEqual(viewer.LIVE_SESSION_WINDOW_SECONDS, 60)
            self.assertTrue(
                viewer.detect_live_session(
                    path,
                    "external",
                    now=updated + viewer.LIVE_SESSION_WINDOW_SECONDS,
                )
            )

    def test_live_session_row_shows_badge_after_timestamp(self) -> None:
        updated = 1_700_000_000
        row = viewer.render_session_row(
            {
                "id": "session-1",
                "name": "Active session",
                "updated": updated,
                "live": True,
            },
            "copilot",
            False,
        )
        self.assertIn('class="live-badge"', row)
        self.assertIn('class="live-badge-dot"', row)
        self.assertIn('class="session-timestamp"', row)
        self.assertIn('class="session session-link  live-session"', row)
        self.assertIn('aria-label="Live session"', row)
        self.assertLess(
            row.index('class="session-timestamp"'),
            row.index('class="live-badge"'),
        )

    def test_inactive_session_row_omits_live_badge(self) -> None:
        row = viewer.render_session_row(
            {
                "id": "session-1",
                "name": "Completed session",
                "updated": 0,
                "live": False,
            },
            "copilot",
            False,
        )
        self.assertNotIn('class="live-badge"', row)


if __name__ == "__main__":
    unittest.main()
