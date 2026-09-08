import unittest

from src import pricing


class PricingTests(unittest.TestCase):
    def test_reasoning_tokens_use_reasoning_rate_instead_of_being_added_twice(self) -> None:
        tokens = {
            "inputTokens": 1_000_000,
            "cacheReadTokens": 1_000_000,
            "cacheWriteTokens": 1_000_000,
            "outputTokens": 1_000_000,
            "reasoningTokens": 400_000,
        }
        # gpt-5.6-luna: 1 + .1 + 1.25 + 3.6 regular output + 2.4 reasoning.
        self.assertAlmostEqual(pricing.cost_for_tokens(tokens, "OpenAI/gpt-5.6-luna"), 8.35)

    def test_unknown_model_has_no_cost(self) -> None:
        self.assertIsNone(pricing.cost_for_tokens({"outputTokens": 10}, "deployment-abc"))

    def test_deployment_path_resolves_to_public_model_name(self) -> None:
        price = pricing.find_model("azure/Azure-APIM/GPT56SOL-GS")
        self.assertIsNotNone(price)
        self.assertEqual(price["model"], "gpt-5.6-sol")

    def test_synthetic_model_has_explicit_zero_cost(self) -> None:
        tokens = {"inputTokens": 0, "outputTokens": 0}
        self.assertEqual(pricing.cost_for_tokens(tokens, "<synthetic>"), 0.0)
        self.assertTrue(all(value == 0.0 for value in pricing.cost_breakdown(tokens, "<synthetic>").values()))

    def _assert_model_costs(self, expected: dict[str, float]) -> None:
        tokens = {
            "inputTokens": 1_000_000,
            "cacheReadTokens": 1_000_000,
            "outputTokens": 1_000_000,
            "reasoningTokens": 0,
        }
        for model, cost in expected.items():
            with self.subTest(model=model):
                self.assertAlmostEqual(pricing.cost_for_tokens(tokens, model), cost)

    def test_github_copilot_models_have_costs(self) -> None:
        self._assert_model_costs({
            "gpt-4o": 13.75,
            "gpt-5-mini": 2.475,
            "gpt-5.1-codex": 11.375,
        })

    def test_openai_codex_models_have_costs(self) -> None:
        self._assert_model_costs({
            "gpt-5.1-codex": 11.375,
            "gpt-5.2-codex": 15.925,
            "gpt-5.6-luna": 7.1,
        })

    def test_anthropic_claude_models_have_costs(self) -> None:
        self._assert_model_costs({
            "claude-haiku-4-5": 3.1,
            "claude-sonnet-4-5": 9.3,
            "claude-opus-4-5": 15.5,
        })

    def test_google_antigravity_models_have_costs(self) -> None:
        self._assert_model_costs({
            "gemini-2.5-flash-lite": 0.51,
            "gemini-3.1-flash-lite": 1.775,
            "gemini-3.5-flash": 10.65,
            "gemini-3.5-flash-lite": 2.83,
            "gemini-3.6-flash": 4.575,
            "gemini-3.7-flash": 4.575,
        })

    def test_microsoft_365_copilot_models_have_costs(self) -> None:
        self._assert_model_costs({
            "gpt-4.1": 10.5,
            "gpt-5": 11.375,
        })

    def _assert_provider_session_cost(self, model: str, expected_cost: float) -> None:
        session = {
            "model": model,
            "tokens": {"inputTokens": 100, "cacheReadTokens": 0, "cacheWriteTokens": 0, "outputTokens": 50, "reasoningTokens": 10},
            "turns": [{
                "tokens": {"inputTokens": 100, "cacheReadTokens": 0, "cacheWriteTokens": 0, "outputTokens": 50, "reasoningTokens": 10},
                "invocations": [{"tokens": {"inputTokens": 100, "cacheReadTokens": 0, "cacheWriteTokens": 0, "outputTokens": 50, "reasoningTokens": 10}}],
            }],
        }
        pricing.apply_costs(session)
        self.assertEqual(session["pricingModel"], model)
        self.assertAlmostEqual(session["costUsd"], expected_cost)
        self.assertEqual(session["costUsd"], session["turns"][0]["costUsd"])
        self.assertEqual(session["turns"][0]["costUsd"], session["turns"][0]["invocations"][0]["costUsd"])

    def test_github_copilot_session_cost_is_applied(self) -> None:
        self._assert_provider_session_cost("gpt-4o", 0.00075)

    def test_openai_codex_session_cost_is_applied(self) -> None:
        self._assert_provider_session_cost("gpt-5.1-codex", 0.000625)

    def test_anthropic_claude_session_cost_is_applied(self) -> None:
        self._assert_provider_session_cost("claude-sonnet-4-5", 0.00069)

    def test_google_antigravity_session_cost_is_applied(self) -> None:
        self._assert_provider_session_cost("gemini-3.8-flash", 0.000045)

    def test_microsoft_365_copilot_session_cost_is_applied(self) -> None:
        self._assert_provider_session_cost("gpt-4o", 0.00075)

if __name__ == "__main__":
    unittest.main()
