import unittest

from session_token_viewer import is_subagent_invocation


class SubagentInvocationTests(unittest.TestCase):
    def test_common_delegation_tools_are_flagged(self) -> None:
        for name in ("Task", "runSubagent", "spawn_agent", "delegate"):
            with self.subTest(name=name):
                self.assertTrue(is_subagent_invocation({"tools": [{"name": name}]}))

    def test_regular_tool_is_not_flagged(self) -> None:
        self.assertFalse(is_subagent_invocation({"tools": [{"name": "read_file"}]}))


if __name__ == "__main__":
    unittest.main()
