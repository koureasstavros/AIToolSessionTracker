import unittest

from session_token_viewer import (
    display_effort_label,
    model_analysis_markup,
    model_usage_breakdown,
    session_reasoning_effort_from_records,
)


class ModelAnalysisTests(unittest.TestCase):
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
