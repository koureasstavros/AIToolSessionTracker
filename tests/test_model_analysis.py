import unittest

from session_token_viewer import model_analysis_markup, model_usage_breakdown


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


if __name__ == "__main__":
    unittest.main()
