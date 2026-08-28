import json
import tempfile
import unittest
from pathlib import Path

from src.providers import github_copilot_provider


class CopilotInvocationGroupingTests(unittest.TestCase):
    def test_tools_are_attached_to_the_assistant_invocation_that_started_them(self) -> None:
        interaction_id = "interaction-1"
        records = [
            {"type": "user.message", "data": {"interactionId": interaction_id, "content": "Inspect the repository"}},
            {"type": "assistant.turn_start", "data": {"interactionId": interaction_id, "turnId": "0"}},
            {"type": "assistant.message", "data": {"interactionId": interaction_id, "turnId": "0", "content": "I will inspect it.", "outputTokens": 10}},
            {"type": "tool.execution_start", "data": {"turnId": "0", "toolCallId": "call-1", "toolName": "read_file", "arguments": {"path": "README.md"}}},
            {"type": "tool.execution_complete", "data": {"interactionId": interaction_id, "turnId": "0", "toolCallId": "call-1", "success": True, "result": "# Project"}},
            {"type": "assistant.turn_start", "data": {"interactionId": interaction_id, "turnId": "1"}},
            {"type": "assistant.message", "data": {"interactionId": interaction_id, "turnId": "1", "content": "Done.", "outputTokens": 5}},
        ]
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory) / "session-1"
            folder.mkdir()
            (folder / "events.jsonl").write_text(
                "\n".join(json.dumps(record) for record in records),
                encoding="utf-8",
            )
            session = github_copilot_provider._read_session_state(folder)

        self.assertEqual(len(session["turns"]), 1)
        turn = session["turns"][0]
        self.assertEqual(len(turn["invocations"]), 2)
        self.assertEqual([len(invocation["tools"]) for invocation in turn["invocations"]], [1, 0])
        self.assertEqual(turn["invocations"][0]["tools"][0]["name"], "read_file")
        self.assertEqual(turn["invocations"][0]["tools"][0]["result"], "# Project")
        self.assertEqual([invocation["tokens"]["outputTokens"] for invocation in turn["invocations"]], [10, 5])

    def test_chat_tool_rounds_include_final_no_tool_metrics(self) -> None:
        request = {
            "requestId": "request-1",
            "message": {"text": "Update the file"},
            "promptTokens": 300,
            "completionTokens": 70,
            "response": [{"value": "Updated."}],
            "result": {
                "metadata": {
                    "promptTokens": 250,
                    "outputTokens": 20,
                    "toolCallRounds": [
                        {"toolCalls": [{"id": "call-1", "name": "apply_patch", "arguments": "{}"}], "response": ""},
                        {"toolCalls": [], "response": "Updated."},
                    ],
                    "toolCallResults": {"call-1": {"content": "done"}},
                }
            },
        }
        document = {"v": {"sessionId": "session-1", "requests": [request]}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session-1.jsonl"
            path.write_text(json.dumps(document), encoding="utf-8")
            session = github_copilot_provider._read_chat(path)

        self.assertEqual(len(session["turns"]), 1)
        turn = session["turns"][0]
        self.assertEqual(turn["tokens"]["inputTokens"], 300)
        self.assertEqual(turn["tokens"]["outputTokens"], 70)
        self.assertEqual(len(turn["invocations"]), 2)
        self.assertEqual([len(invocation["tools"]) for invocation in turn["invocations"]], [1, 0])
        self.assertEqual(turn["invocations"][1]["tokens"]["inputTokens"], 250)
        self.assertEqual(turn["invocations"][1]["tokens"]["outputTokens"], 20)


if __name__ == "__main__":
    unittest.main()
