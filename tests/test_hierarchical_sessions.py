import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.providers import anthropic_claude_provider


class ClaudeHierarchyTests(unittest.TestCase):
    def test_subagent_is_attached_to_parent_and_usage_is_aggregated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            project = home / ".claude" / "projects" / "project"
            subagents = project / "subagents"
            subagents.mkdir(parents=True)
            parent = project / "parent.jsonl"
            child = subagents / "agent-worker.jsonl"
            parent.write_text(json.dumps({"sessionId": "parent", "type": "user", "message": {"role": "user", "content": "Parent"}}) + "\n", encoding="utf-8")
            child.write_text(json.dumps({"sessionId": "child", "parentSessionId": "parent", "type": "assistant", "model": "claude-sonnet-4-5", "message": {"role": "assistant", "content": "Delegated", "usage": {"input_tokens": 10, "output_tokens": 5}}}) + "\n", encoding="utf-8")
            with patch("pathlib.Path.home", return_value=home):
                entries = anthropic_claude_provider.index(home)
                session = anthropic_claude_provider.details(entries[0])

        self.assertEqual(len(entries), 1)
        self.assertEqual(len(entries[0]["_children"]), 1)
        self.assertEqual(len(session["subagents"]), 1)
        self.assertEqual(session["subagentTokens"]["outputTokens"], 5)
        self.assertEqual(session["tokens"]["outputTokens"], 5)


if __name__ == "__main__":
    unittest.main()
