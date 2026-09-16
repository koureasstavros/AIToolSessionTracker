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

        with patch("session_token_viewer.PROVIDER_ADAPTERS", {"copilot": Adapter()}), patch(
            "session_token_viewer.source_mode", return_value="local"
        ):
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

    def test_timeline_keeps_cost_axis_inside_svg_viewbox(self) -> None:
        markup = render_timeline_svg(
            {"2026-09-09": {"tokens": 120.0, "cost": 0.5}},
            provider="claude",
        )

        self.assertIn('x="956"', markup)
        self.assertIn('x="935"', markup)

    def test_statistics_currency_labels_show_two_decimals(self) -> None:
        self.assertEqual(fmt_unit(1.2, currency=True), "$1.20")
        self.assertEqual(fmt_unit(1_234.5, currency=True), "$1.23K")

    def test_view_tabs_use_the_loading_overlay(self) -> None:
        self.assertIn("a.view-tab", PAGE)
        self.assertIn("loading('Loading session data…')", PAGE)

    def test_settings_lists_model_costs_before_mappings(self) -> None:
        from session_token_viewer import render_settings_page

        markup = render_settings_page()

        self.assertLess(markup.index("Model costs"), markup.index("Model mappings"))

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

    def test_statistics_renders_while_details_are_still_loading(self) -> None:
        markup = render(
            Path("."),
            None,
            provider="copilot",
            view="statistics",
            sessions_override=[],
            statistics_override=[],
            scan_bootstrap="poll",
        )

        self.assertIn("Statistics", markup)
        self.assertNotIn("Scanning local sessions", markup)

    def test_statistics_sidebar_shows_scan_indicator(self) -> None:
        markup = render(
            Path("."),
            None,
            provider="copilot",
            view="statistics",
            sessions_override=[],
            statistics_override=[],
            scan_bootstrap="poll",
        )

        self.assertIn("scan-indicator scan-high-level", markup)

    def test_scan_indicator_turns_red_for_full_transcript_warming(self) -> None:
        markup = render(
            Path("."),
            None,
            provider="copilot",
            sessions_override=[],
            statistics_override=[],
            scan_bootstrap="poll",
            scan_phase="full",
        )

        self.assertIn("scan-indicator scan-full-transcripts", markup)
        self.assertIn("Loading full transcripts", markup)

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

    def test_scan_poll_updates_sidebar_and_detail_without_replacing_app(self) -> None:
        handler_source = inspect.getsource(Handler.do_GET)

        self.assertIn("currentList.replaceWith(incomingList)", handler_source)
        self.assertIn("currentDetail.replaceWith(incomingDetail)", handler_source)
        self.assertNotIn("document.querySelector('.app').replaceWith", handler_source)

    def test_full_transcript_poll_does_not_replace_sidebar_list(self) -> None:
        handler_source = inspect.getsource(Handler.do_GET)

        self.assertIn("fullLoading", handler_source)
        self.assertIn("if(!fullLoading)", handler_source)

    def test_sidebar_shows_scan_indicator_during_progressive_scan(self) -> None:
        markup = render(
            Path("."),
            None,
            provider="copilot",
            sessions_override=[],
            statistics_override=[],
            scan_bootstrap="poll",
        )

        self.assertIn("scan-indicator scan-high-level", markup)
        self.assertIn("Scanning session list", markup)

    def test_background_scan_publishes_all_sessions_sequentially(self) -> None:
        entries = [
            {"id": "first", "name": "first", "updated": 1, "_has_data": True},
            {"id": "second", "name": "second", "updated": 2, "_has_data": True},
        ]
        seen = []
        detail_calls = []

        class Adapter:
            def index(self, _root):
                seen.append("index")
                return [dict(entry) for entry in entries]

            def details(self, summary):
                detail_calls.append(summary["id"])
                return summary

        adapters = {provider: Adapter() for provider in ("copilot", "codex", "claude", "antigravity", "m365_copilot")}
        manager = BackgroundScanManager(Path("."))
        with patch("session_token_viewer.PROVIDER_ADAPTERS", adapters), patch(
            "session_token_viewer.source_mode", return_value="local"
        ):
            manager.start()
            manager._thread.join(timeout=2)

        sessions, statuses = manager.snapshot("copilot")
        self.assertEqual({item["id"] for item in sessions}, {"first", "second"})
        self.assertEqual(len(seen), 5)
        self.assertEqual(detail_calls, [])
        self.assertTrue(all(value == "complete" for value in statuses.values()))

    def test_background_scan_hides_unknown_sessions_unless_show_empty(self) -> None:
        entries = [
            {"id": "known", "name": "known", "updated": 2, "_has_data": True},
            {"id": "unknown", "name": "unknown", "updated": 1},
        ]

        class Adapter:
            def index(self, _root):
                return [dict(entry) for entry in entries]

            def details(self, summary):
                raise AssertionError(f"details loaded during scan: {summary['id']}")

        adapters = {provider: Adapter() for provider in ("copilot", "codex", "claude", "antigravity", "m365_copilot")}
        manager = BackgroundScanManager(Path("."))
        with patch("session_token_viewer.PROVIDER_ADAPTERS", adapters), patch(
            "session_token_viewer.source_mode", return_value="local"
        ):
            manager.start()
            manager._thread.join(timeout=2)

        self.assertEqual([item["id"] for item in manager.snapshot("copilot")[0]], ["known"])
        self.assertEqual(
            {item["id"] for item in manager.snapshot("copilot", show_empty=True)[0]},
            {"known", "unknown"},
        )

    def test_statistics_details_load_one_session_at_a_time(self) -> None:
        manager = BackgroundScanManager(Path("."))
        first = {"id": "first", "provider": "copilot", "_has_data": True, "_source": Path("first")}
        second = {"id": "second", "provider": "copilot", "_has_data": True, "_source": Path("second")}
        manager._sessions["copilot"] = [first, second]
        manager._statistics_sessions["copilot"] = [("copilot", first), ("copilot", second)]

        with patch("session_token_viewer.load_session_details", side_effect=[{"id": "first"}, {"id": "second"}]) as load:
            self.assertTrue(manager.load_next_statistics_detail())
            self.assertEqual(load.call_count, 1)
            self.assertEqual(manager.all_sessions(loaded_only=True)[0][1]["id"], "first")
            self.assertFalse(manager.statistics_complete())
            self.assertTrue(manager.load_next_statistics_detail())
            self.assertTrue(manager.statistics_complete())

    def test_model_statistics_breakdown_is_cached_on_loaded_session(self) -> None:
        from session_token_viewer import model_usage_breakdown

        session = {"model": "gpt-test", "tokens": {key: 0 for key in ("inputTokens", "cacheReadTokens", "cacheWriteTokens", "outputTokens", "reasoningTokens")}, "turns": []}
        with patch("session_token_viewer.pricing.find_model", side_effect=AssertionError("recomputed model lookup")):
            first = model_usage_breakdown(session)
            session["_model_usage_breakdown"] = first
            self.assertIs(model_usage_breakdown(session), first)

    def test_statistics_can_group_by_reasoning_effort(self) -> None:
        from session_token_viewer import statistics_group_key

        self.assertEqual(
            statistics_group_key({"reasoningEffort": "high"}, "effort", "copilot", {}),
            "high",
        )
        markup = render(Path("."), None, view="statistics", group="effort", statistics_override=[])
        self.assertIn('group=effort', markup)
        self.assertIn(">Effort<", markup)
        self.assertNotIn("Unknown effort", markup)

    def test_statistics_scan_indicator_precedes_group_menu(self) -> None:
        markup = render(Path("."), None, view="statistics", statistics_override=[], scan_bootstrap="poll")

        self.assertLess(markup.index("scan-indicator"), markup.index("Group by"))

    def test_effort_breakdown_is_granular(self) -> None:
        from session_token_viewer import model_usage_breakdown

        session = {
            "model": "gpt-test",
            "tokens": {key: 0 for key in ("inputTokens", "cacheReadTokens", "cacheWriteTokens", "outputTokens", "reasoningTokens")},
            "turns": [{
                "reasoningEffort": "high",
                "tokens": {key: 0 for key in ("inputTokens", "cacheReadTokens", "cacheWriteTokens", "outputTokens", "reasoningTokens")},
                "invocations": [{"reasoningEffort": "low", "model": "gpt-test", "tokens": {"outputTokens": 5}}],
            }],
        }

        labels = {row["model"] for row in model_usage_breakdown(session, "effort")}
        self.assertEqual(labels, {"low"})

    def test_scan_handler_handles_cancelled_poll_connections(self) -> None:
        handler_source = inspect.getsource(Handler.do_GET)

        self.assertIn("ConnectionAbortedError", handler_source)
        self.assertIn("browser can cancel a polling request", handler_source)

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
