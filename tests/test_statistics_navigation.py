import inspect
import unittest
from pathlib import Path
from unittest.mock import patch

from session_token_viewer import BackgroundScanManager, Handler, PAGE, fmt_unit, load_session_index, render, render_timeline_svg


class StatisticsNavigationTests(unittest.TestCase):
    def test_session_index_pages_newest_first(self) -> None:
        entries = [
            {"id": "old", "name": "old", "updated": 1, "_has_data": True},
            {"id": "new", "name": "new", "updated": 3, "_has_data": True},
            {"id": "middle", "name": "middle", "updated": 2, "_has_data": True},
        ]

        class Adapter:
            def index(self, _root):
                return [dict(entry) for entry in entries]

            def details(self, summary):
                return summary

        with patch("session_token_viewer.PROVIDER_ADAPTERS", {"copilot": Adapter()}):
            page = load_session_index(Path("."), "copilot", limit=1, offset=0)
            older = load_session_index(Path("."), "copilot", limit=1, offset=1)

        self.assertEqual([item["id"] for item in page], ["new"])
        self.assertEqual([item["id"] for item in older], ["middle"])

    def test_timeline_targets_statistics_navigation_and_provider(self) -> None:
        markup = render_timeline_svg(
            {"2026-09-09": {"tokens": 120.0, "cost": 0.5}},
            provider="claude",
        )

        self.assertIn('class="stats-navigation"', markup)
        self.assertIn("provider=claude", markup)
        self.assertIn("view=statistics", markup)

    def test_statistics_currency_labels_show_two_decimals(self) -> None:
        self.assertEqual(fmt_unit(1.2, currency=True), "$1.20")
        self.assertEqual(fmt_unit(1_234.5, currency=True), "$1.23K")

    def test_view_tabs_use_the_loading_overlay(self) -> None:
        self.assertIn("a.view-tab", PAGE)
        self.assertIn("loading('Loading session data…')", PAGE)

    def test_delete_removes_session_row_without_refresh(self) -> None:
        self.assertIn("'X-Requested-With':'XMLHttpRequest'", PAGE)
        self.assertIn("new URLSearchParams(new FormData(form))", PAGE)
        self.assertIn("'Content-Type':'application/x-www-form-urlencoded;charset=UTF-8'", PAGE)
        self.assertIn("if(row)row.remove()", PAGE)
        self.assertIn("document.querySelectorAll('.session-row').length", PAGE)

    def test_import_redirect_requests_a_fresh_scan(self) -> None:
        handler_source = inspect.getsource(Handler.do_POST)

        self.assertIn("type(self).scan_manager = None", handler_source)
        self.assertIn("'refresh': 1", handler_source)

    def test_all_toggle_targets_only_top_level_turn_panels(self) -> None:
        render_source = inspect.getsource(render)
        self.assertIn('target==="all"?".turn-message,.turn-invocations"', render_source)
        self.assertNotIn('target==="all"?"details"', render_source)

    def test_render_can_build_shell_without_loading_index(self) -> None:
        with patch("session_token_viewer.load_session_index", side_effect=AssertionError("synchronous scan")), patch(
            "session_token_viewer.render_statistics", side_effect=AssertionError("synchronous statistics")
        ):
            markup = render(Path("."), None, provider="copilot", view="statistics", sessions_override=[], statistics_override=[], scan_bootstrap="poll")

        self.assertIn("AI Tool Session Tracker", markup)
        self.assertNotIn("synchronous scan", markup)

    def test_operational_shell_uses_centered_scanning_view(self) -> None:
        markup = render(
            Path("."),
            None,
            provider="copilot",
            view="operational",
            sessions_override=[],
            statistics_override=[],
            scan_bootstrap="poll",
        )

        self.assertIn('class="detail progressive-scan ', markup)
        self.assertIn("Scanning local sessions", markup)

    def test_operational_keeps_scanning_view_after_first_session_appears(self) -> None:
        markup = render(
            Path("."),
            None,
            provider="copilot",
            view="operational",
            sessions_override=[{"id": "new", "name": "new", "updated": 1_700_000_000, "provider": "copilot"}],
            statistics_override=[],
            scan_bootstrap="poll",
        )

        self.assertIn("Scanning local sessions", markup)
        self.assertNotIn("Select a session", markup)

    def test_background_scan_publishes_all_sessions_sequentially(self) -> None:
        entries = [
            {"id": "first", "name": "first", "updated": 1, "_has_data": True},
            {"id": "second", "name": "second", "updated": 2, "_has_data": True},
        ]
        seen = []

        class Adapter:
            def index(self, _root):
                seen.append("index")
                return [dict(entry) for entry in entries]

            def details(self, summary):
                return summary

        adapters = {provider: Adapter() for provider in ("copilot", "codex", "claude", "antigravity", "m365_copilot")}
        manager = BackgroundScanManager(Path("."))
        with patch("session_token_viewer.PROVIDER_ADAPTERS", adapters):
            manager.start()
            manager._thread.join(timeout=2)

        sessions, statuses = manager.snapshot("copilot")
        self.assertEqual({item["id"] for item in sessions}, {"first", "second"})
        self.assertEqual(len(seen), 5)
        self.assertTrue(all(value == "complete" for value in statuses.values()))

    def test_background_scan_removes_deleted_session_from_cached_results(self) -> None:
        manager = BackgroundScanManager(Path("."))
        deleted = {"id": "deleted", "name": "deleted", "updated": 2}
        kept = {"id": "kept", "name": "kept", "updated": 1}
        manager._sessions["copilot"] = [deleted, kept]
        manager._all_sessions["copilot"] = [deleted, kept]
        manager._statistics_sessions["copilot"] = [("copilot", deleted), ("copilot", kept)]

        manager.remove_session("copilot", "deleted")

        visible, _ = manager.snapshot("copilot")
        all_sessions, _ = manager.snapshot("copilot", show_empty=True)
        self.assertEqual([item["id"] for item in visible], ["kept"])
        self.assertEqual([item["id"] for item in all_sessions], ["kept"])
        self.assertEqual([item[1]["id"] for item in manager.all_sessions(True)], ["kept"])

    def test_scan_manager_deletes_from_fresh_summary_and_cache(self) -> None:
        manager = BackgroundScanManager(Path("."))
        cached = {"id": "session-1", "provider": "copilot"}
        fresh = {"id": "session-1", "provider": "copilot", "_source": Path("fresh")}
        manager._sessions["copilot"] = [cached]
        manager._all_sessions["copilot"] = [cached]

        with patch("session_token_viewer.load_session_index", return_value=[fresh]), patch(
            "session_token_viewer.delete_session"
        ) as delete:
            deleted = manager.delete_provider_session("copilot", "session-1")

        self.assertTrue(deleted)
        delete.assert_called_once_with(fresh)
        self.assertEqual(manager.snapshot("copilot")[0], [])

    def test_scan_manager_retries_transient_delete_failure(self) -> None:
        manager = BackgroundScanManager(Path("."))
        summary = {"id": "session-1", "provider": "copilot", "_source": Path("source")}

        with patch("session_token_viewer.load_session_index", return_value=[summary]), patch(
            "session_token_viewer.delete_session", side_effect=[PermissionError("locked"), None]
        ) as delete, patch("session_token_viewer.time.sleep") as sleep:
            deleted = manager.delete_provider_session("copilot", "session-1")

        self.assertTrue(deleted)
        self.assertEqual(delete.call_count, 2)
        sleep.assert_called_once_with(0.25)


if __name__ == "__main__":
    unittest.main()
