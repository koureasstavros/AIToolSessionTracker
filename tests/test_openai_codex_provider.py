import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.providers import openai_codex_provider


class CodexInvocationGroupingTests(unittest.TestCase):
    def test_source_is_identified_from_originator(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            sessions = home / ".codex" / "sessions"
            sessions.mkdir(parents=True)
            path = sessions / "rollout-cli.jsonl"
            path.write_text(json.dumps({
                "type": "session_meta",
                "payload": {"id": "cli-session", "originator": "codex_vscode", "source": "vscode"},
            }) + "\n", encoding="utf-8")
            with patch("pathlib.Path.home", return_value=home):
                entries = openai_codex_provider.index(home)

        self.assertEqual(entries[0]["_source_label"], "Extension")

    def test_conflicting_originators_are_mixed(self) -> None:
        records = [
            {"type": "session_meta", "payload": {"id": "mixed-session", "originator": "codex_vscode"}},
            {"type": "event_msg", "payload": {"originator": "codex_work_desktop"}},
        ]
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            sessions = home / ".codex" / "sessions"
            sessions.mkdir(parents=True)
            path = sessions / "rollout-mixed.jsonl"
            path.write_text("\n".join(json.dumps(record) for record in records), encoding="utf-8")
            with patch("pathlib.Path.home", return_value=home):
                entries = openai_codex_provider.index(home)

        self.assertEqual(entries[0]["_source_label"], "Mixed")

    def test_codex_tui_originator_is_cli(self) -> None:
        self.assertEqual(
            openai_codex_provider._surface_from_records([
                {"type": "session_meta", "payload": {"originator": "codex-tui", "source": "cli"}},
            ]),
            "CLI",
        )

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

    def test_spawned_rollout_is_nested_under_owning_invocation(self) -> None:
        parent_id = "11111111-1111-4111-8111-111111111111"
        child_id = "22222222-2222-4222-8222-222222222222"
        call_id = "call-agent"
        parent_records = [
            {"type": "session_meta", "payload": {"id": parent_id, "source": "vscode"}},
            {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "turn-1"}},
            {"type": "response_item", "payload": {"type": "message", "role": "user", "content": "Delegate work"}},
            {"type": "response_item", "payload": {"type": "function_call", "call_id": call_id, "name": "spawn_agent", "arguments": "{}"}},
            {"type": "response_item", "payload": {"type": "function_call_output", "call_id": call_id, "output": json.dumps({"agent_id": child_id, "nickname": "Worker"})}},
            {"type": "event_msg", "payload": {"type": "token_count", "info": {"last_token_usage": {"input_tokens": 100, "cached_input_tokens": 60, "cache_write_input_tokens": 30, "output_tokens": 10}}}},
            {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "turn-1"}},
        ]
        child_records = [
            {"type": "session_meta", "payload": {"id": child_id, "source": {"subagent": {"thread_spawn": {"parent_thread_id": parent_id, "depth": 1, "agent_nickname": "Worker"}}}}},
            {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "child-turn"}},
            {"type": "response_item", "payload": {"type": "message", "role": "user", "content": "Do work"}},
            {"type": "event_msg", "payload": {"type": "token_count", "info": {"last_token_usage": {"input_tokens": 50, "cached_input_tokens": 20, "cache_write_input_tokens": 20, "output_tokens": 5}}}},
            {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "child-turn"}},
        ]
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            sessions = home / ".codex" / "sessions" / "2026" / "09" / "10"
            sessions.mkdir(parents=True)
            parent = sessions / f"rollout-{parent_id}.jsonl"
            child = sessions / f"rollout-{child_id}.jsonl"
            parent.write_text("\n".join(json.dumps(record) for record in parent_records), encoding="utf-8")
            child.write_text("\n".join(json.dumps(record) for record in child_records), encoding="utf-8")
            with patch("pathlib.Path.home", return_value=home):
                entries = openai_codex_provider.index(home)
                session = openai_codex_provider.details(entries[0])

        self.assertEqual(len(entries), 1)
        self.assertEqual(len(entries[0]["_children"]), 1)
        self.assertEqual(len(session["subagents"]), 1)
        tool = session["turns"][0]["invocations"][0]["tools"][0]
        self.assertEqual(tool["subagent"]["agentDescription"], "Worker")
        self.assertEqual(tool["subagent"]["tokens"]["inputTokens"], 10)
        self.assertEqual(tool["subagent"]["tokens"]["outputTokens"], 5)


if __name__ == "__main__":
    unittest.main()
