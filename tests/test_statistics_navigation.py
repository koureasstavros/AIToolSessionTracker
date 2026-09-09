import unittest

from session_token_viewer import PAGE, render_timeline_svg


class StatisticsNavigationTests(unittest.TestCase):
    def test_timeline_targets_statistics_navigation_and_provider(self) -> None:
        markup = render_timeline_svg(
            {"2026-09-09": {"tokens": 120.0, "cost": 0.5}},
            provider="claude",
        )

        self.assertIn('class="stats-navigation"', markup)
        self.assertIn("provider=claude", markup)
        self.assertIn("view=statistics", markup)

    def test_view_tabs_use_the_loading_overlay(self) -> None:
        self.assertIn("a.view-tab", PAGE)
        self.assertIn("loading('Loading session data…')", PAGE)


if __name__ == "__main__":
    unittest.main()
