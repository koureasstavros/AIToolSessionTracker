import json
import tempfile
import unittest
from pathlib import Path

from src.providers import openai_codex_provider


class CodexInvocationGroupingTests(unittest.TestCase):
    def test_tools_are_grouped_as_invocations_in_one_logical_turn(self) -> None:
        records = [
            {"type": "session_meta", "payload": {"id": "session-1"}},
            {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "turn-1"}},
            {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Inspect the repo"}]}},
            {"type": "response_item", "payload": {"type": "function_call", "call_id": "call-1", "name": "list_files", "arguments": "{}"}},
            {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "call-1", "output": "README.md"}},
            {"type": "event_msg", "payload": {"type": "token_count", "info": {"last_token_usage": {"input_tokens": 100, "cached_input_tokens": 70, "cache_write_input_tokens": 20, "output_tokens": 10}}}},
            {"type": "response_item", "payload": {"type": "function_call", "call_id": "call-2", "name": "read_file", "arguments": "{\"path\":\"README.md\"}"}},
            {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "call-2", "output": "# Project"}},
            {"type": "event_msg", "payload": {"type": "token_count", "info": {"last_token_usage": {"input_tokens": 120, "cached_input_tokens": 80, "cache_write_input_tokens": 30, "output_tokens": 20}}}},
            {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "turn-1"}},
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session-1.jsonl"
            path.write_text("\n".join(json.dumps(record) for record in records), encoding="utf-8")
            session = openai_codex_provider.details({"_source": path})

        self.assertEqual(len(session["turns"]), 1)
        turn = session["turns"][0]
        self.assertEqual(len(turn["invocations"]), 2)
        self.assertEqual([tool["name"] for tool in turn["tools"]], ["list_files", "read_file"])
        self.assertEqual(turn["invocations"][0]["tools"][0]["result"], "README.md")
        self.assertEqual(turn["invocations"][1]["tools"][0]["result"], "# Project")
        self.assertEqual(turn["tokens"]["inputTokens"], 20)
        self.assertEqual([invocation["tokens"]["inputTokens"] for invocation in turn["invocations"]], [10, 10])


if __name__ == "__main__":
    unittest.main()
