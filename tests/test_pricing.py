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

    def test_apply_costs_annotates_session_turn_and_invocation(self) -> None:
        session = {
            "model": "claude-sonnet-4-5",
            "tokens": {"inputTokens": 100, "cacheReadTokens": 0, "cacheWriteTokens": 0, "outputTokens": 50, "reasoningTokens": 10},
            "turns": [{
                "tokens": {"inputTokens": 100, "cacheReadTokens": 0, "cacheWriteTokens": 0, "outputTokens": 50, "reasoningTokens": 10},
                "invocations": [{"tokens": {"inputTokens": 100, "cacheReadTokens": 0, "cacheWriteTokens": 0, "outputTokens": 50, "reasoningTokens": 10}}],
            }],
        }
        pricing.apply_costs(session)
        self.assertEqual(session["pricingModel"], "claude-sonnet-4-5")
        self.assertGreater(session["costUsd"], 0)
        self.assertEqual(session["costUsd"], session["turns"][0]["costUsd"])
        self.assertEqual(session["turns"][0]["costUsd"], session["turns"][0]["invocations"][0]["costUsd"])


if __name__ == "__main__":
    unittest.main()
