import unittest

from session_token_viewer import invocation_tools, is_subagent_invocation


class SubagentInvocationTests(unittest.TestCase):
    def test_common_delegation_tools_are_flagged(self) -> None:
        for name in ("Task", "runSubagent", "spawn_agent", "delegate"):
            with self.subTest(name=name):
                self.assertTrue(is_subagent_invocation({"tools": [{"name": name}]}))

    def test_regular_tool_is_not_flagged(self) -> None:
        self.assertFalse(is_subagent_invocation({"tools": [{"name": "read_file"}]}))

    def test_no_tool_invocation_displays_its_assistant_response(self) -> None:
        markup = invocation_tools({
            "tools": [],
            "assistant": ["The background agent has completed."],
        })

        self.assertIn("The background agent has completed.", markup)
        self.assertNotIn("no tool calls", markup)

    def test_linked_subagent_displays_its_own_context(self) -> None:
        markup = invocation_tools({
            "tools": [{
                "name": "spawn_agent",
                "status": "completed",
                "arguments": {"prompt": "Do delegated work"},
                "result": "Delegated result",
                "subagent": {
                    "agentDescription": "Worker",
                    "model": "gpt-test",
                    "costUsd": 0.00330665,
                    "tokens": {"inputTokens": 1},
                    "turns": [{
                        "internalInstructions": [{
                            "name": "Codex developer instructions",
                            "content": "Child-only <context>",
                        }],
                    }],
                },
            }],
        })

        self.assertIn("Subagent context", markup)
        self.assertIn("Codex developer instructions", markup)
        self.assertIn("Child-only &lt;context&gt;", markup)
        self.assertIn("$0.003307", markup)
        self.assertLess(markup.index("Subagent context"), markup.index("Arguments"))
        self.assertLess(markup.index("Arguments"), markup.index("Result"))
        self.assertLess(markup.index("Result"), markup.index("invocation-usage"))


if __name__ == "__main__":
    unittest.main()
