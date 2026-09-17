import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from session_token_viewer import normalize_session_data
from src.providers import cognition_devin_local_provider


class CognitionDevinProviderTests(unittest.TestCase):
    def test_surface_detection_prefers_desktop_metadata(self) -> None:
        rows = [{"role": "system", "content": "You are running inside Devin Desktop."}]
        self.assertEqual(cognition_devin_local_provider._surface({}, rows, Path("sessions.db")), "Desktop")

    def _database(self, directory: str) -> Path:
        path = Path(directory) / "sessions.db"
        connection = sqlite3.connect(path)
        connection.executescript(
            """
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY, working_directory TEXT NOT NULL,
                backend_type TEXT NOT NULL, model TEXT NOT NULL,
                agent_mode TEXT NOT NULL, created_at INTEGER NOT NULL,
                last_activity_at INTEGER NOT NULL, title TEXT,
                main_chain_id INTEGER, shell_last_seen_index INTEGER DEFAULT 0,
                cogs_json TEXT, workspace_dirs TEXT, hidden INTEGER NOT NULL DEFAULT 0,
                metadata TEXT
            );
            CREATE TABLE message_nodes (
                row_id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
                node_id INTEGER NOT NULL, parent_node_id INTEGER,
                chat_message TEXT NOT NULL, created_at INTEGER NOT NULL, metadata TEXT
            );
            CREATE TABLE prompt_history (id INTEGER PRIMARY KEY, content TEXT, timestamp INTEGER, session_id TEXT);
            CREATE TABLE rendered_commits (id INTEGER PRIMARY KEY, session_id TEXT, sequence_number INTEGER, rendered_html TEXT, created_at INTEGER);
            CREATE TABLE tool_call_state (session_id TEXT, tool_call_id TEXT, tool_call_json TEXT, tool_call_update_json TEXT);
            CREATE TABLE subagent_heads (session_id TEXT, agent_id TEXT, chain_node_id INTEGER, updated_at INTEGER);
            """
        )
        session_id = "devin-session"
        connection.execute(
            "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (session_id, r"C:\work\app", "devin", "swe-1-6-slow", "accept-edits", 100, 200, "", 2, 0, "[]", "[]", 0, "{}"),
        )
        user = {"message_id": "11111111-2222-3333-4444-555555555555", "role": "user", "content": "Inspect the app", "metadata": {"is_user_input": True}}
        assistant = {
            "message_id": "a1", "role": "assistant", "content": "I inspected it.",
            "metadata": {"metrics": {"input_tokens": 42, "output_tokens": 8, "cache_read_tokens": 12, "cache_creation_tokens": 3}},
        }
        for node, message in enumerate((user, assistant, assistant)):
            connection.execute(
                "INSERT INTO message_nodes (session_id, node_id, parent_node_id, chat_message, created_at, metadata) VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, node, node - 1 if node else None, json.dumps(message), 100 + node, None),
            )
        connection.commit()
        connection.close()
        return path

    def test_index_and_details_use_persisted_usage_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = self._database(directory)
            with patch.dict("os.environ", {"COGNITION_DEVIN_DB": str(database)}):
                entries = cognition_devin_local_provider.index(database)
                normalized = normalize_session_data(entries[0])
                details = cognition_devin_local_provider.details(normalized)

        self.assertEqual(entries[0]["name"], "Inspect the app")
        self.assertEqual(entries[0]["id"], "11111111-2222-3333-4444-555555555555")
        self.assertEqual(entries[0]["model"], "swe-1-6-slow")
        self.assertEqual(normalized["_devin_session_id"], "devin-session")
        self.assertEqual(entries[0]["_source_label"], "CLI")
        self.assertEqual(details["surface"], "CLI")
        self.assertEqual(len(details["turns"]), 1)
        self.assertEqual(details["turns"][0]["user"], "Inspect the app")
        self.assertEqual(details["turns"][0]["assistant"], ["I inspected it."])
        self.assertEqual(details["turns"][0]["invocations"][0]["tokens"]["inputTokens"], 42)
        self.assertEqual(details["turns"][0]["invocations"][0]["tokens"]["outputTokens"], 8)
        self.assertEqual(details["tokens"]["inputTokens"], 42)
        self.assertEqual(details["tokens"]["cacheReadTokens"], 12)
        self.assertEqual(details["tokens"]["cacheWriteTokens"], 3)
        self.assertEqual(details["tokens"]["outputTokens"], 8)
        self.assertEqual(details["tokenFlags"], [])


if __name__ == "__main__":
    unittest.main()
