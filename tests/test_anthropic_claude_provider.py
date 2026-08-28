import json
import tempfile
import unittest
from pathlib import Path

from src.providers import anthropic_claude_provider


class ClaudeInvocationGroupingTests(unittest.TestCase):
    def test_assistant_messages_become_invocations_with_nested_tools_and_usage(self) -> None:
        first_usage = {"input_tokens": 2, "cache_read_input_tokens": 100, "cache_creation_input_tokens": 20, "output_tokens": 12}
        final_usage = {"input_tokens": 3, "cache_read_input_tokens": 110, "cache_creation_input_tokens": 5, "output_tokens": 30}
        records = [
            {"type": "user", "uuid": "user-1", "sessionId": "session-1", "message": {"role": "user", "content": [{"type": "text", "text": "Inspect the project"}]}},
            {"type": "assistant", "uuid": "assistant-text-1", "message": {"id": "message-1", "role": "assistant", "content": [{"type": "text", "text": "I will inspect it."}], "usage": first_usage}},
            {"type": "assistant", "uuid": "assistant-tool-1", "message": {"id": "message-1", "role": "assistant", "content": [{"type": "tool_use", "id": "tool-1", "name": "Read", "input": {"file_path": "README.md"}}], "usage": first_usage}},
            {"type": "user", "uuid": "result-1", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tool-1", "content": "# Project"}]}},
            {"type": "assistant", "uuid": "assistant-final", "message": {"id": "message-2", "role": "assistant", "content": [{"type": "text", "text": "The project is a viewer."}], "usage": final_usage}},
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session-1.jsonl"
            path.write_text("\n".join(json.dumps(record) for record in records), encoding="utf-8")
            session = anthropic_claude_provider.details({"_source": path})

        self.assertEqual(len(session["turns"]), 1)
        turn = session["turns"][0]
        self.assertEqual(len(turn["invocations"]), 2)
        self.assertEqual([len(invocation["tools"]) for invocation in turn["invocations"]], [1, 0])
        self.assertEqual(turn["invocations"][0]["tools"][0]["result"], "# Project")
        self.assertEqual(turn["invocations"][0]["tokens"]["outputTokens"], 12)
        self.assertEqual(turn["invocations"][1]["tokens"]["outputTokens"], 30)
        self.assertEqual(turn["tokens"]["outputTokens"], 42)


if __name__ == "__main__":
    unittest.main()
