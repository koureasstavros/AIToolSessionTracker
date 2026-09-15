import unittest

from session_token_viewer import (
    best_practices_findings,
    best_practices_markup,
    display_effort_label,
    model_analysis_markup,
    model_usage_breakdown,
    session_reasoning_effort_from_records,
)


class ModelAnalysisTests(unittest.TestCase):
    def test_best_practices_detects_model_effort_context_and_idle_cache_misses(self) -> None:
        def turn(timestamp: float, model: str, effort: str, instructions: str, cache_read: int) -> dict:
            return {
                "timestamp": timestamp,
                "model": model,
                "reasoningEffort": effort,
                "internalInstructions": [{"name": "MCP", "content": instructions}],
                "tokens": {"inputTokens": 1_000, "cacheReadTokens": cache_read, "cacheWriteTokens": 0},
            }

        session = {
            "model": "claude-sonnet-4-5",
            "reasoningEffort": "low",
            "turns": [
                turn(1_000, "claude-sonnet-4-5", "low", "tools-a", 100),
                turn(1_301, "claude-haiku-4-5", "high", "tools-b", 0),
            ],
        }

        findings = best_practices_findings(session)
        self.assertEqual({finding["kind"] for finding in findings}, {"model", "effort", "context", "idle"})
        self.assertTrue(all(finding["turn"] == 2 for finding in findings))
        self.assertTrue(all(finding["cost"] is not None for finding in findings))

    def test_best_practices_requires_evidence_of_a_cache_miss(self) -> None:
        session = {
            "model": "claude-sonnet-4-5",
            "turns": [
                {"timestamp": 1_000, "model": "claude-sonnet-4-5", "tokens": {"cacheReadTokens": 10}},
                {"timestamp": 1_301, "model": "claude-haiku-4-5", "tokens": {"cacheReadTokens": None}},
            ],
        }

        self.assertEqual(best_practices_findings(session), [])

    def test_best_practices_uses_invocation_models_when_turn_is_aggregated(self) -> None:
        session = {
            "model": "claude-sonnet-4-5",
            "turns": [
                {"model": "claude-sonnet-4-5", "tokens": {"cacheReadTokens": 100}, "invocations": [
                    {"model": "claude-sonnet-4-5", "tokens": {"inputTokens": 100, "cacheReadTokens": 100}},
                ]},
                {"model": "claude-sonnet-4-5", "tokens": {"cacheReadTokens": 0}, "invocations": [
                    {"model": "claude-haiku-4-5", "tokens": {"inputTokens": 100, "cacheReadTokens": 0}},
                ]},
            ],
        }

        findings = best_practices_findings(session)
        self.assertTrue(any(finding["kind"] == "model" for finding in findings))
        self.assertTrue(all(finding["turn"] == 2 for finding in findings))

    def test_best_practices_uses_zero_based_persisted_turn_label(self) -> None:
        session = {
            "model": "claude-sonnet-4-5",
            "turns": [
                {"turn_index": 0, "timestamp": 1_000, "model": "claude-sonnet-4-5", "tokens": {"cacheReadTokens": 10}},
                {"turn_index": 1, "timestamp": 1_301, "model": "claude-haiku-4-5", "tokens": {"inputTokens": 100, "cacheReadTokens": 0}},
            ],
        }

        findings = best_practices_findings(session)
        self.assertTrue(any(finding["kind"] == "model" and finding["turn"] == 1 for finding in findings))

    def test_copilot_best_practices_force_one_based_turn_labels(self) -> None:
        session = {
            "provider": "copilot",
            "model": "claude-sonnet-4-5",
            "turns": [
                {"turn_index": 0, "timestamp": 1_000, "model": "claude-sonnet-4-5", "tokens": {"cacheReadTokens": 10}},
                {"turn_index": 1, "timestamp": 1_301, "model": "claude-haiku-4-5", "tokens": {"inputTokens": 100, "cacheReadTokens": 0}},
            ],
        }

        findings = best_practices_findings(session)
        self.assertTrue(any(finding["kind"] == "model" and finding["turn"] == 2 for finding in findings))

    def test_best_practices_markup_shows_findings_and_recommendation(self) -> None:
        session = {
            "model": "claude-sonnet-4-5",
            "turns": [
                {"timestamp": 1_000, "model": "claude-sonnet-4-5", "tokens": {"cacheReadTokens": 10}},
                {"timestamp": 1_001, "model": "claude-haiku-4-5", "tokens": {"inputTokens": 100, "cacheReadTokens": 0}},
            ],
        }

        markup = best_practices_markup(session)
        self.assertIn("Best practices", markup)
        self.assertIn("Model changed", markup)
        self.assertIn("Model stability", markup)
        self.assertIn("Keep the model", markup)

    def test_breakdown_groups_parent_and_agent_usage_by_model_once(self) -> None:
        parent_tokens = {
            "inputTokens": 10,
            "cacheReadTokens": 0,
            "cacheWriteTokens": 0,
            "outputTokens": 20,
            "reasoningTokens": 5,
        }
        agent_tokens = {
            "inputTokens": 3,
            "cacheReadTokens": 0,
            "cacheWriteTokens": 0,
            "outputTokens": 7,
            "reasoningTokens": 2,
        }
        aggregate_tokens = {
            key: parent_tokens[key] + agent_tokens[key]
            for key in parent_tokens
        }
        agent = {
            "model": "claude-haiku-4-5",
            "agentModel": "claude-haiku-4-5",
            "ownTokens": dict(agent_tokens),
            "tokens": dict(agent_tokens),
            "subagents": [],
        }
        session = {
            "model": "claude-sonnet-4-5",
            "pricingModel": "Mixed",
            "tokens": dict(aggregate_tokens),
            "turns": [{
                "model": "claude-sonnet-4-5",
                "tokens": dict(aggregate_tokens),
                "invocations": [{
                    "model": "claude-sonnet-4-5",
                    "tokens": dict(parent_tokens),
                    "tools": [{"subagent": agent}],
                }],
            }],
            "subagents": [agent],
        }

        rows = {row["model"]: row for row in model_usage_breakdown(session)}

        self.assertEqual(set(rows), {"claude-sonnet-4-5", "claude-haiku-4-5"})
        self.assertEqual(rows["claude-sonnet-4-5"]["tokens"], parent_tokens)
        self.assertEqual(rows["claude-haiku-4-5"]["tokens"], agent_tokens)

    def test_markup_renders_model_names_tokens_and_costs(self) -> None:
        session = {
            "model": "claude-sonnet-4-5",
            "tokens": {
                "inputTokens": 10,
                "cacheReadTokens": 0,
                "cacheWriteTokens": 0,
                "outputTokens": 20,
                "reasoningTokens": 5,
            },
            "turns": [],
        }

        markup = model_analysis_markup(session)

        self.assertIn("Model analysis", markup)
        self.assertIn("claude-sonnet-4-5", markup)
        self.assertIn("15 /", markup)
        self.assertIn("$", markup)

    def test_effort_has_an_independent_label(self) -> None:
        self.assertEqual(
            display_effort_label("high"),
            "Effort: high",
        )

    def test_session_effort_is_mixed_for_models_with_different_efforts(self) -> None:
        records = [
            {"data": {"model": "model-a", "reasoningEffort": "low"}},
            {"data": {"model": "model-b", "reasoningEffort": "high"}},
        ]

        self.assertEqual(session_reasoning_effort_from_records(records), "Mixed")

    def test_session_effort_is_not_mixed_for_equal_model_efforts(self) -> None:
        records = [
            {"data": {"model": "model-a", "reasoningEffort": "medium"}},
            {"data": {"model": "model-b", "reasoningEffort": "medium"}},
        ]

        self.assertEqual(session_reasoning_effort_from_records(records), "medium")


if __name__ == "__main__":
    unittest.main()
