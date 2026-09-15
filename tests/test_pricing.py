import tempfile
import unittest
import sqlite3
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from src.common import source_pricing as pricing


class PricingTests(unittest.TestCase):
    def test_model_costs_seed_from_bundled_catalog_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "AI-Tool-Session-Tracker-config.db"
            costs = pricing.load_model_costs(database_path)
            with closing(sqlite3.connect(database_path)) as database:
                count = database.execute("SELECT COUNT(*) FROM model_costs").fetchone()[0]

        self.assertGreater(count, 1)
        self.assertTrue(any(row["model"] == "gpt-4o" for row in costs))

    def test_model_costs_add_missing_bundled_entries_on_later_load(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "AI-Tool-Session-Tracker-config.db"
            pricing.load_model_costs(database_path)
            with closing(sqlite3.connect(database_path)) as database:
                database.execute("DELETE FROM model_costs WHERE model = 'gpt-6-astra'")
                database.commit()

            costs = pricing.load_model_costs(database_path)

            self.assertTrue(any(row["model"] == "gpt-6-astra" for row in costs))

    def test_deleted_bundled_model_stays_deleted_after_save_and_reload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "AI-Tool-Session-Tracker-config.db"
            costs = pricing.load_model_costs(database_path)
            costs = [row for row in costs if row["model"] != "gpt-6-astra"]
            pricing.save_model_costs(costs, database_path)

            reloaded = pricing.load_model_costs(database_path)

            self.assertFalse(any(row["model"] == "gpt-6-astra" for row in reloaded))

    def test_custom_model_costs_are_saved_and_used_for_pricing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "AI-Tool-Session-Tracker-config.db"
            pricing.save_model_costs([{
                "vendor": "Test", "model": "test-model", "input": 2, "cache_read": 0.5,
                "cache_write": 1, "output": 4, "reasoning": 6,
            }], database_path)
            with patch.object(pricing, "mapping_config_path", return_value=database_path):
                self.assertTrue(any(row["model"] == "test-model" for row in pricing.load_model_costs()))
                self.assertEqual(pricing.cost_for_tokens({"inputTokens": 1_000_000, "outputTokens": 1_000_000}, "test-model"), 6.0)

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

    def test_missing_cache_rates_fall_back_to_input_rate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "AI-Tool-Session-Tracker-config.db"
            pricing.save_model_costs([{
                "vendor": "Test", "model": "cache-fallback", "input": 2,
                "cache_read": None, "cache_write": None, "output": 4, "reasoning": None,
            }], database_path)
            with patch.object(pricing, "mapping_config_path", return_value=database_path):
                tokens = {
                    "inputTokens": 1_000_000,
                    "cacheReadTokens": 1_000_000,
                    "cacheWriteTokens": 1_000_000,
                }
                self.assertAlmostEqual(pricing.cost_for_tokens(tokens, "cache-fallback"), 6.0)

    def test_separate_reasoning_output_is_priced_without_double_subtraction(self) -> None:
        tokens = {"outputTokens": 600_000, "reasoningTokens": 400_000}

        self.assertAlmostEqual(
            pricing.cost_for_tokens(tokens, "gpt-5.6-luna", output_includes_reasoning=False),
            6.0,
        )

    def test_unknown_model_has_no_cost(self) -> None:
        self.assertIsNone(pricing.cost_for_tokens({"outputTokens": 10}, "deployment-abc"))

    def test_explicit_deployment_mapping_resolves_model(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "AI-Tool-Session-Tracker-config.db"
            pricing.save_model_mappings({"TEST-GS": "gpt-5.6-luna"}, config_path)
            with closing(sqlite3.connect(config_path)) as database:
                self.assertEqual(
                    database.execute("SELECT model FROM model_mappings WHERE deployment = 'TEST-GS'").fetchone()[0],
                    "gpt-5.6-luna",
                )
            with patch.object(pricing, "mapping_config_path", return_value=config_path):
                self.assertEqual(pricing.mapped_model("TEST-GS"), "gpt-5.6-luna")
                self.assertEqual(pricing.find_model("TEST-GS")["model"], "gpt-5.6-luna")

    def test_model_mappings_seed_from_bundled_catalog_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "AI-Tool-Session-Tracker-config.db"

            mappings = pricing.load_model_mappings(database_path)

            self.assertEqual(mappings["GPT56LUNA-GS"], "gpt-5.6-luna")
            with closing(sqlite3.connect(database_path)) as database:
                self.assertEqual(
                    database.execute(
                        "SELECT value FROM tracker_metadata WHERE key = 'model_mapping_json_seeded'"
                    ).fetchone()[0],
                    "1",
                )

    def test_model_mappings_add_missing_bundled_entries_on_later_load(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "AI-Tool-Session-Tracker-config.db"
            pricing.load_model_mappings(database_path)
            with closing(sqlite3.connect(database_path)) as database:
                database.execute("DELETE FROM model_mappings WHERE deployment = 'GPT56LUNA-GS'")
                database.commit()

            mappings = pricing.load_model_mappings(database_path)

            self.assertEqual(mappings["GPT56LUNA-GS"], "gpt-5.6-luna")

    def test_deleted_bundled_mapping_stays_deleted_after_save_and_reload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "AI-Tool-Session-Tracker-config.db"
            mappings = pricing.load_model_mappings(database_path)
            mappings.pop("GPT56LUNA-GS")
            pricing.save_model_mappings(mappings, database_path)

            reloaded = pricing.load_model_mappings(database_path)

            self.assertNotIn("GPT56LUNA-GS", reloaded)

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

    def test_subagent_session_cost_is_applied_recursively(self) -> None:
        child_tokens = {
            "inputTokens": 100,
            "cacheReadTokens": 0,
            "cacheWriteTokens": 0,
            "outputTokens": 50,
            "reasoningTokens": 10,
        }
        child = {
            "model": "gpt-5.6-luna",
            "tokens": dict(child_tokens),
            "turns": [{"tokens": dict(child_tokens), "invocations": []}],
        }
        parent = {
            "model": "gpt-5.6-luna",
            "tokens": dict(child_tokens),
            "turns": [],
            "subagents": [child],
        }

        pricing.apply_costs(parent)

        self.assertAlmostEqual(child["costUsd"], 0.0004)
        self.assertEqual(child["pricingModel"], "gpt-5.6-luna")

    def test_mixed_model_session_uses_each_agents_actual_model_rate(self) -> None:
        parent_tokens = {
            "inputTokens": 100,
            "cacheReadTokens": 0,
            "cacheWriteTokens": 0,
            "outputTokens": 50,
            "reasoningTokens": 10,
        }
        agent_tokens = {
            "inputTokens": 20,
            "cacheReadTokens": 0,
            "cacheWriteTokens": 0,
            "outputTokens": 10,
            "reasoningTokens": 2,
        }
        aggregate_tokens = {
            key: parent_tokens[key] + agent_tokens[key]
            for key in parent_tokens
        }
        agent = {
            "model": "claude-haiku-4-5",
            "tokens": dict(agent_tokens),
            "turns": [{"tokens": dict(agent_tokens), "invocations": []}],
        }
        session = {
            "model": "claude-sonnet-4-5",
            "tokens": dict(aggregate_tokens),
            "turns": [{
                "model": "claude-sonnet-4-5",
                "tokens": dict(aggregate_tokens),
                "invocations": [{
                    "tokens": dict(parent_tokens),
                    "tools": [{"subagent": agent}],
                }],
            }],
            "subagents": [agent],
        }

        pricing.apply_costs(session)

        expected = (
            pricing.cost_for_tokens(parent_tokens, "claude-sonnet-4-5")
            + pricing.cost_for_tokens(agent_tokens, "claude-haiku-4-5")
        )
        self.assertAlmostEqual(session["costUsd"], expected)
        self.assertAlmostEqual(session["turns"][0]["costUsd"], expected)
        self.assertEqual(session["pricingModel"], "Mixed")
        self.assertAlmostEqual(sum(session["costBreakdown"].values()), expected)

    def test_anthropic_claude_session_cost_is_applied(self) -> None:
        self._assert_provider_session_cost("claude-sonnet-4-5", 0.00069)

    def test_google_antigravity_session_cost_is_applied(self) -> None:
        self._assert_provider_session_cost("gemini-3.8-flash", 0.000045)

    def test_microsoft_365_copilot_session_cost_is_applied(self) -> None:
        self._assert_provider_session_cost("gpt-4o", 0.00075)

if __name__ == "__main__":
    unittest.main()
