import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.providers import google_antigravity_provider


class GoogleAntigravityProviderTests(unittest.TestCase):
    def test_identity_extracts_user_request_and_uuid(self) -> None:
        record = {
            "id": "12345678-1234-1234-1234-123456789abc",
            "content": "<USER_REQUEST>\nBuild a weather dashboard\n</USER_REQUEST>\n<ADDITIONAL_METADATA>...</ADDITIONAL_METADATA>",
        }
        session_id, name = google_antigravity_provider.identity(record, "fallback")
        self.assertEqual(session_id, "12345678-1234-1234-1234-123456789abc")
        self.assertEqual(name, "Build a weather dashboard")

    def test_details_parses_turns_invocations_and_tools(self) -> None:
        steps = [
            {
                "step_index": 0,
                "source": "USER_EXPLICIT",
                "type": "USER_INPUT",
                "content": "<USER_REQUEST>\nAnalyze repository structure\n</USER_REQUEST>\n<ADDITIONAL_METADATA>\nActive Document: c:\\projects\\app\\main.py\n</ADDITIONAL_METADATA>",
            },
            {
                "step_index": 1,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "content": "I will examine the directory.",
                "thinking": "Listing the repository files first.",
                "tool_calls": [
                    {
                        "name": "list_dir",
                        "args": {"DirectoryPath": "c:\\projects\\app"},
                    }
                ],
            },
            {
                "step_index": 2,
                "source": "MODEL",
                "type": "LIST_DIRECTORY",
                "status": "DONE",
                "content": "main.py\nREADME.md\n",
            },
            {
                "step_index": 3,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "content": "Here is the project overview.",
            },
        ]
        with tempfile.TemporaryDirectory() as directory:
            conv_id = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
            conv_dir = Path(directory) / conv_id
            logs_dir = conv_dir / ".system_generated" / "logs"
            logs_dir.mkdir(parents=True)
            transcript = logs_dir / "transcript.jsonl"
            transcript.write_text("\n".join(json.dumps(step) for step in steps), encoding="utf-8")

            summary = {"_source": transcript, "id": conv_id, "name": "Analyze repository structure"}
            details = google_antigravity_provider.details(summary)

        self.assertEqual(details["id"], conv_id)
        self.assertEqual(details["name"], "Analyze repository structure")
        self.assertEqual(len(details["turns"]), 1)

        turn = details["turns"][0]
        self.assertEqual(turn["user"], "Analyze repository structure")
        self.assertEqual(turn["files"][0]["name"], "main.py")
        self.assertEqual(turn["files"][0]["path"], "c:\\projects\\app\\main.py")
        self.assertEqual(len(turn["invocations"]), 2)

        # Invocations
        inv1 = turn["invocations"][0]
        self.assertEqual(inv1["assistant"], ["I will examine the directory."])
        self.assertEqual(inv1["thinking"], ["Listing the repository files first."])
        self.assertEqual(len(inv1["tools"]), 1)
        self.assertEqual(inv1["tools"][0]["name"], "list_dir")
        self.assertEqual(inv1["tools"][0]["status"], "completed")
        self.assertEqual(inv1["tools"][0]["result"], "main.py\nREADME.md\n")

        inv2 = turn["invocations"][1]
        self.assertEqual(inv2["assistant"], ["Here is the project overview."])

        # Verify token estimation metrics are populated
        self.assertGreater(turn["tokens"]["inputTokens"], 0)
        self.assertGreater(turn["tokens"]["outputTokens"], 0)
        self.assertGreater(turn["tokens"]["reasoningTokens"], 0)
        self.assertGreater(details["tokens"]["inputTokens"], 0)
        self.assertGreater(details["tokens"]["outputTokens"], 0)
        self.assertEqual(details["tokens"]["inputTokens"], turn["tokens"]["inputTokens"])
        self.assertEqual(details["tokens"]["outputTokens"], turn["tokens"]["outputTokens"])

    def test_subagent_tool_marks_invocation(self) -> None:
        steps = [
            {
                "step_index": 0,
                "type": "USER_INPUT",
                "content": "<USER_REQUEST>Run tests</USER_REQUEST>",
            },
            {
                "step_index": 1,
                "type": "PLANNER_RESPONSE",
                "content": "Delegating to subagent",
                "tool_calls": [
                    {
                        "name": "browser_subagent",
                        "args": {"Task": "Navigate to login"},
                    }
                ],
            },
            {
                "step_index": 2,
                "type": "BROWSER_SUBAGENT",
                "status": "DONE",
                "content": "Subagent finished",
            },
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "transcript.jsonl"
            path.write_text("\n".join(json.dumps(step) for step in steps), encoding="utf-8")
            details = google_antigravity_provider.details({"_source": path, "id": "test-subagent"})

        self.assertEqual(len(details["turns"]), 1)
        invocation = details["turns"][0]["invocations"][0]
        self.assertTrue(invocation.get("isSubagent"))

    def test_index_discovers_brain_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            conv_id = "55555555-4444-3333-2222-111111111111"
            conv_dir = base / conv_id
            logs_dir = conv_dir / ".system_generated" / "logs"
            logs_dir.mkdir(parents=True)
            transcript = logs_dir / "transcript.jsonl"
            transcript.write_text(
                json.dumps({
                    "step_index": 0,
                    "type": "USER_INPUT",
                    "content": "<USER_REQUEST>My prompt title</USER_REQUEST>",
                }) + "\n",
                encoding="utf-8",
            )
            with patch("src.providers.google_antigravity_provider._candidate_roots", return_value=[base]):
                entries = google_antigravity_provider.index(base)

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["id"], conv_id)
        self.assertEqual(entries[0]["name"], "My prompt title")
        self.assertTrue(entries[0]["_has_data"])

    def test_index_discovers_cli_sessions_and_labels_them(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cli_root = Path(directory) / "antigravity-cli"
            session_dir = cli_root / "sessions" / "cli-session"
            session_dir.mkdir(parents=True)
            transcript = session_dir / "transcript.jsonl"
            transcript.write_text(
                json.dumps({
                    "type": "USER_INPUT",
                    "content": "<USER_REQUEST>CLI session</USER_REQUEST>",
                }) + "\n",
                encoding="utf-8",
            )
            with patch("src.providers.google_antigravity_provider._candidate_roots", return_value=[cli_root]):
                entries = google_antigravity_provider.index(cli_root)

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["name"], "CLI session")
        self.assertEqual(entries[0]["_source_label"], "CLI")

    def test_index_ignores_cli_session_chunks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cli_root = Path(directory) / "antigravity-cli"
            session_dir = cli_root / "brain" / "session-id" / ".system_generated" / "logs"
            chunks_dir = session_dir / "chunks" / "transcript"
            chunks_dir.mkdir(parents=True)
            (session_dir / "transcript.jsonl").write_text(
                json.dumps({
                    "type": "USER_INPUT",
                    "content": "<USER_REQUEST>One CLI session</USER_REQUEST>",
                }) + "\n",
                encoding="utf-8",
            )
            (chunks_dir / "00000000.jsonl").write_text(
                json.dumps({
                    "type": "USER_INPUT",
                    "content": "<USER_REQUEST>Duplicate chunk</USER_REQUEST>",
                }) + "\n",
                encoding="utf-8",
            )

            with patch("src.providers.google_antigravity_provider._candidate_roots", return_value=[cli_root]):
                entries = google_antigravity_provider.index(cli_root)

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["id"], "session-id")

    def test_source_labels_ide_transcripts_as_ide(self) -> None:
        source = Path.home() / ".gemini" / "antigravity-ide" / "brain" / "session" / ".system_generated" / "logs" / "transcript.jsonl"
        self.assertEqual(google_antigravity_provider.tool({"_source": source}), "IDE")

    def test_source_labels_desktop_transcripts_as_desktop(self) -> None:
        source = Path.home() / ".gemini" / "antigravity" / "brain" / "session" / ".system_generated" / "logs" / "transcript.jsonl"
        self.assertEqual(google_antigravity_provider.tool({"_source": source}), "Desktop")

    def test_delete_removes_session_directory_and_db(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            brain_dir = base / "brain"
            convs_dir = base / "conversations"
            brain_dir.mkdir()
            convs_dir.mkdir()

            conv_id = "77777777-8888-9999-aaaa-bbbbbbbbbbbb"
            conv_folder = brain_dir / conv_id
            logs_dir = conv_folder / ".system_generated" / "logs"
            logs_dir.mkdir(parents=True)
            transcript = logs_dir / "transcript.jsonl"
            transcript.write_text('{"type":"USER_INPUT"}\n', encoding="utf-8")

            db_file = convs_dir / f"{conv_id}.db"
            db_file.write_bytes(b"dummy db")

            summary = {"_source": transcript, "id": conv_id}
            google_antigravity_provider.delete(summary)

            self.assertFalse(conv_folder.exists())
            self.assertFalse(db_file.exists())


if __name__ == "__main__":
    unittest.main()
