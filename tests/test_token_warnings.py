import unittest

from session_token_viewer import best_practices_findings, normalize_session_data, token_cards, token_warning, turn_timestamp, usage_from


class TokenWarningTests(unittest.TestCase):
    def test_normalization_preserves_detected_surface(self) -> None:
        session = normalize_session_data({
            "id": "session",
            "_surface": "Desktop",
            "_source_label": "Desktop",
        })

        self.assertEqual(session["_surface"], "Desktop")
        self.assertEqual(session["_source_label"], "Desktop")

    def test_turn_timestamp_reads_iso_and_millisecond_values(self) -> None:
        self.assertGreater(turn_timestamp({"raw": ['{"timestamp":"2026-09-10T12:00:00Z"}']}), 0)
        self.assertEqual(turn_timestamp({"timestamp": 1_000_000_000_000}), 1_000_000_000)

    def test_canonical_reasoning_tokens_are_preserved(self) -> None:
        self.assertEqual(usage_from({"reasoningTokens": 17})["reasoningTokens"], 17)
        self.assertEqual(usage_from({"reasoningTokens": 0})["reasoningTokens"], 0)

    def test_antigravity_marks_session_turn_and_invocation_estimated(self) -> None:
        session = normalize_session_data({
            "provider": "google_antigravity",
            "id": "session",
            "tokenFlags": ["estimated"],
            "tokens": {key: None for key in ("inputTokens", "cacheReadTokens", "cacheWriteTokens", "outputTokens", "reasoningTokens")},
            "turns": [{"tokens": {key: None for key in ("inputTokens", "cacheReadTokens", "cacheWriteTokens", "outputTokens", "reasoningTokens")}, "invocations": [{"tokens": {key: None for key in ("inputTokens", "cacheReadTokens", "cacheWriteTokens", "outputTokens", "reasoningTokens")}}]}],
        })

        self.assertEqual(session["tokenFlags"], ["estimated"])
        self.assertEqual(session["turns"][0]["tokenFlags"], ["estimated"])
        self.assertEqual(session["turns"][0]["invocations"][0]["tokenFlags"], ["estimated"])
        self.assertIn("estimated", token_warning(session["tokenFlags"]))

    def test_copilot_flags_missing_categories_and_multi_turn_allocation(self) -> None:
        session = normalize_session_data({
            "provider": "github_copilot",
            "id": "session",
            "tokenFlags": ["estimated"],
            "tokens": {"inputTokens": 10, "outputTokens": 20},
            "turns": [{"tokens": {}, "invocations": []}, {"tokens": {}, "invocations": []}],
        })

        self.assertEqual(set(session["tokenFlags"]), {"uncategorizedInput", "uncategorizedOutput", "estimated"})
        self.assertIn("not accurate", token_warning(session["tokenFlags"]))
        self.assertIn("estimated", token_warning(session["tokenFlags"]))

    def test_complete_single_turn_copilot_session_has_no_warning(self) -> None:
        tokens = {
            "inputTokens": 10,
            "cacheReadTokens": 20,
            "cacheWriteTokens": 30,
            "outputTokens": 40,
            "reasoningTokens": 5,
        }
        session = normalize_session_data({
            "provider": "github_copilot",
            "id": "session",
            "tokens": tokens,
            "turns": [{"tokens": tokens, "invocations": [{"tokens": tokens}]}],
        })

        self.assertEqual(session["tokenFlags"], [])
        self.assertEqual(session["turns"][0]["tokenFlags"], [])
        self.assertEqual(session["turns"][0]["invocations"][0]["tokenFlags"], [])
        self.assertEqual(token_warning(session["tokenFlags"]), "")

    def test_null_and_zero_category_values_are_still_present(self) -> None:
        tokens = {
            "inputTokens": None,
            "cacheReadTokens": 0,
            "cacheWriteTokens": None,
            "outputTokens": 12,
            "reasoningTokens": 0,
        }
        session = normalize_session_data({"provider": "github_copilot", "id": "session", "tokens": tokens})

        self.assertEqual(session["tokenFlags"], [])

    def test_missing_token_object_is_uncategorized(self) -> None:
        session = normalize_session_data({"provider": "github_copilot", "id": "session"})

        self.assertEqual(set(session["tokenFlags"]), {"uncategorizedInput", "uncategorizedOutput"})

    def test_warning_icon_is_rendered_with_tooltip(self) -> None:
        markup = token_cards({"inputTokens": 10}, token_flags=["uncategorized"])

        self.assertIn("class=\"token-warning\"", markup)
        self.assertIn("Price is not accurate", markup)
        self.assertIn("⚠", markup)

    def test_output_card_shows_non_thinking_output(self) -> None:
        markup = token_cards({"outputTokens": 1_412, "reasoningTokens": 547})

        self.assertIn("865 /", markup)
        self.assertNotIn("1,412 /", markup)

    def test_cache_ttl_costs_are_shown_in_the_cache_write_card(self) -> None:
        markup = token_cards(
            {
                "cacheWriteTokens": 2_000_000,
                "cacheWrite5mTokens": 1_000_000,
                "cacheWrite1hTokens": 1_000_000,
            },
            "claude-haiku-4-5",
        )

        self.assertIn("3.25", markup)

    def test_claude_warns_when_one_hour_cache_ttl_has_expired(self) -> None:
        session = {
            "provider": "anthropic_claude",
            "model": "claude-sonnet-4-5",
            "turns": [
                {
                    "model": "claude-sonnet-4-5",
                    "timestamp": "2026-09-25T10:00:00Z",
                    "tokens": {"cacheWrite1hTokens": 100},
                    "invocations": [],
                },
                {
                    "model": "claude-sonnet-4-5",
                    "timestamp": "2026-09-25T11:00:01Z",
                    "tokens": {"inputTokens": 100, "cacheReadTokens": 0},
                    "invocations": [],
                },
            ],
        }

        findings = best_practices_findings(session)

        self.assertTrue(any(finding["kind"] == "cache_ttl" for finding in findings))

    def test_claude_does_not_warn_before_one_hour_cache_ttl(self) -> None:
        session = {
            "provider": "anthropic_claude",
            "model": "claude-sonnet-4-5",
            "turns": [
                {
                    "model": "claude-sonnet-4-5",
                    "timestamp": "2026-09-25T10:00:00Z",
                    "tokens": {"cacheWrite1hTokens": 100},
                    "invocations": [],
                },
                {
                    "model": "claude-sonnet-4-5",
                    "timestamp": "2026-09-25T10:59:59Z",
                    "tokens": {"inputTokens": 100, "cacheReadTokens": 0},
                    "invocations": [],
                },
            ],
        }

        findings = best_practices_findings(session)

        self.assertFalse(any(finding["kind"] == "cache_ttl" for finding in findings))

    def test_api_cache_ttl_data_warns_after_one_hour_for_github_route(self) -> None:
        session = {
            "provider": "github_copilot",
            "model": "provider-model-123",
            "turns": [
                {
                    "model": "provider-model-123",
                    "timestamp": "2026-09-25T10:00:00Z",
                    "tokens": {"cacheWrite1hTokens": 100},
                    "invocations": [],
                },
                {
                    "model": "anthropic/claude-sonnet-4-5",
                    "timestamp": "2026-09-25T11:00:01Z",
                    "tokens": {"inputTokens": 100, "cacheReadTokens": 0},
                    "invocations": [],
                },
            ],
        }

        findings = best_practices_findings(session)

        self.assertTrue(any(finding["kind"] == "cache_ttl" for finding in findings))


if __name__ == "__main__":
    unittest.main()
