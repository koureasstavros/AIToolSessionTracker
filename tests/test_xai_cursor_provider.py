import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from src.providers import xai_cursor_local_provider


class XaiCursorProviderTests(unittest.TestCase):
    def test_surface_detection_supports_extension_cli_and_desktop(self) -> None:
        self.assertEqual(xai_cursor_local_provider._surface([{"role": "user", "content": "VS Code extension"}]), "Extension")
        self.assertEqual(xai_cursor_local_provider._surface([{"role": "user", "content": "Cursor CLI"}]), "CLI")
        self.assertEqual(xai_cursor_local_provider._surface([{"role": "user", "content": "Desktop Cursor"}]), "Desktop")

    def test_index_discovers_xai_cursor_transcript_and_title(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session_id = "11111111-2222-3333-4444-555555555555"
            transcript_dir = root / "empty-window" / "agent-transcripts" / session_id
            transcript_dir.mkdir(parents=True)
            path = transcript_dir / f"{session_id}.jsonl"
            path.write_text(json.dumps({
                "role": "user",
                "message": {"content": [{"type": "text", "text": "<user_query>Inspect the app</user_query>"}]},
            }) + "\n", encoding="utf-8")
            with patch.dict("os.environ", {"XAI_CURSOR_ROOT": str(root)}):
                entries = xai_cursor_local_provider.index(root)

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["id"], session_id)
        self.assertEqual(entries[0]["name"], "Inspect the app")
        self.assertTrue(entries[0]["_has_data"])
        self.assertEqual(entries[0]["_source_label"], "Desktop")

    def test_details_parses_messages_tools_and_estimates_tokens(self) -> None:
        session_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        records = [
            {"role": "user", "message": {"content": [{"type": "text", "text": "<user_query>Run the tests</user_query>"}]}},
            {"role": "assistant", "message": {"content": [
                {"type": "text", "text": "I will run them."},
                {"type": "tool_call", "id": "call-1", "name": "run_terminal", "arguments": {"command": "pytest"}},
            ]}, "model": "grok-4.6"},
            {"role": "tool", "message": {"content": [{"type": "text", "text": "3 passed"}]}},
            {"type": "turn_ended", "status": "success"},
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / f"{session_id}.jsonl"
            path.write_text("\n".join(json.dumps(record) for record in records), encoding="utf-8")
            result = xai_cursor_local_provider.details({"_source": path, "id": session_id, "name": "Run the tests"})

        self.assertEqual(result["id"], session_id)
        self.assertEqual(result["model"], "grok-4.6")
        self.assertEqual(result["turns"][0]["user"], "Run the tests")
        invocation = result["turns"][0]["invocations"][0]
        self.assertEqual(invocation["assistant"], ["I will run them."])
        self.assertEqual(invocation["tools"][0]["status"], "completed")
        self.assertEqual(invocation["tools"][0]["result"], "3 passed")
        self.assertIn("estimated", result["tokenFlags"])
        self.assertGreater(result["tokens"]["inputTokens"], 0)
        self.assertGreater(result["tokens"]["outputTokens"], 0)


if __name__ == "__main__":
    unittest.main()
