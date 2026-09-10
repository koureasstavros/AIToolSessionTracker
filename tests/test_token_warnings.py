import unittest

from session_token_viewer import normalize_session_data, token_cards, token_warning, turn_timestamp, usage_from


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

    def test_thinking_tokens_are_normalized_as_reasoning_tokens(self) -> None:
        self.assertEqual(usage_from({"thinking_tokens": 17})["reasoningTokens"], 17)
        self.assertEqual(usage_from({"thinkingTokens": 19})["reasoningTokens"], 19)
        self.assertEqual(usage_from({"output_tokens_details": {"thinking_tokens": 0}})["reasoningTokens"], 0)

    def test_antigravity_marks_session_turn_and_invocation_estimated(self) -> None:
        session = normalize_session_data({
            "provider": "antigravity",
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
            "provider": "copilot",
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
            "provider": "copilot",
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
        session = normalize_session_data({"provider": "copilot", "id": "session", "tokens": tokens})

        self.assertEqual(session["tokenFlags"], [])

    def test_missing_token_object_is_uncategorized(self) -> None:
        session = normalize_session_data({"provider": "copilot", "id": "session"})

        self.assertEqual(set(session["tokenFlags"]), {"uncategorizedInput", "uncategorizedOutput"})

    def test_warning_icon_is_rendered_with_tooltip(self) -> None:
        markup = token_cards({"inputTokens": 10}, token_flags=["uncategorized"])

        self.assertIn("class=\"token-warning\"", markup)
        self.assertIn("Price is not accurate", markup)
        self.assertIn("⚠", markup)


if __name__ == "__main__":
    unittest.main()
