import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from src.providers import anthropic_claude_provider, github_copilot_provider, m365_copilot_provider, openai_codex_provider


class SourceArchiveTests(unittest.TestCase):
    def test_each_provider_round_trips_source_files(self) -> None:
        providers = (
            ("codex", openai_codex_provider),
            ("claude", anthropic_claude_provider),
            ("m365_copilot", m365_copilot_provider),
        )
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "transcript.jsonl"
            source.write_text('{"role":"user","content":"hello"}\n', encoding="utf-8")
            for provider, adapter in providers:
                archive = base / f"{provider}.zip"
                summary = {"_source": source}
                with patch("pathlib.Path.home", return_value=base / provider):
                    adapter.export_source_files(summary, archive)
                    with zipfile.ZipFile(archive) as exported:
                        manifest = json.loads(exported.read("manifest.json"))
                    self.assertEqual(manifest["provider"], provider)
                    adapter.import_source_files(archive, base / "viewer-root")

    def test_copilot_exports_session_state_and_injects_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "session-1"
            source.mkdir()
            (source / "events.jsonl").write_text('{"type":"user.message"}\n', encoding="utf-8")
            archive = base / "copilot.zip"
            summary = {"_source": source, "_kind": "copilot-session-state"}
            github_copilot_provider.export_source_files(summary, archive)
            destination = base / "workspace-storage"
            written = github_copilot_provider.import_source_files(archive, destination)
            self.assertEqual(len(written), 1)
            self.assertTrue((destination / "imported" / "session-1" / "events.jsonl").exists())

    def test_copilot_does_not_export_session_database(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            database = base / "session-store.db"
            database.write_bytes(b"SQLite format 3")
            archive = base / "copilot-db.zip"
            github_copilot_provider.export_source_files(
                {"_source": database, "_kind": "copilot-db"}, archive
            )
            with zipfile.ZipFile(archive) as exported:
                self.assertNotIn("session-store.db", exported.namelist())

    def test_copilot_merged_session_exports_one_richest_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            state = base / "state-session"
            state.mkdir()
            (state / "events.jsonl").write_text(
                '{"type":"user.message","data":{"content":"state prompt"}}\n',
                encoding="utf-8",
            )
            chat = base / "chat.jsonl"
            chat.write_text(
                json.dumps({"v": {"sessionId": "chat-id", "requests": [{"message": {"text": "chat prompt"}, "response": [{"value": "chat answer"}]}]}}),
                encoding="utf-8",
            )
            archive = base / "merged.zip"
            github_copilot_provider.export_source_files(
                {"_sources": [
                    {"_source": state, "_kind": "copilot-session-state"},
                    {"_source": chat, "_kind": "copilot-chat"},
                ]},
                archive,
            )
            with zipfile.ZipFile(archive) as exported:
                source_members = [name for name in exported.namelist() if name.startswith("sources/")]
            self.assertEqual(len(source_members), 1)


if __name__ == "__main__":
    unittest.main()
