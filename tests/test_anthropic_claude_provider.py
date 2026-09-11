import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import session_token_viewer
from src.common import source_pricing as pricing
from src.providers import anthropic_claude_provider


class ClaudeInvocationGroupingTests(unittest.TestCase):
    def test_shared_transcript_without_surface_metadata_is_mixed(self) -> None:
        self.assertEqual(
            anthropic_claude_provider.tool({"_source": Path("session.jsonl")}),
            "Mixed",
        )

    def test_transcript_entrypoint_identifies_cli_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            project = home / ".claude" / "projects" / "project"
            project.mkdir(parents=True)
            path = project / "cli-session.jsonl"
            path.write_text(
                json.dumps({
                    "sessionId": "cli-session",
                    "type": "user",
                    "entrypoint": "cli",
                    "message": {"role": "user", "content": "Hello from CLI"},
                }) + "\n",
                encoding="utf-8",
            )
            with patch("pathlib.Path.home", return_value=home):
                entries = anthropic_claude_provider.index(home)

        self.assertEqual(entries[0]["_source_label"], "CLI")

    def test_shared_project_transcript_uses_desktop_marker_and_audit_is_discovered(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            project = home / ".claude" / "projects" / "project"
            project.mkdir(parents=True)
            session_id = "desktop-session"
            transcript = project / f"{session_id}.jsonl"
            transcript.write_text(
                json.dumps({
                    "sessionId": session_id,
                    "type": "user",
                    "message": {"role": "user", "content": "Hello from Desktop"},
                }) + "\n",
                encoding="utf-8",
            )
            (project / f"{session_id}.desktop-released.json").write_text(
                json.dumps({"v": 1, "reason": "delete"}),
                encoding="utf-8",
            )
            sessions = home / ".claude" / "sessions"
            sessions.mkdir(parents=True)
            (sessions / "extension.json").write_text(
                json.dumps({"sessionId": session_id, "entrypoint": "claude-vscode"}),
                encoding="utf-8",
            )
            audit = home / "AppData" / "Local" / "Claude-3p" / "local-agent-mode-sessions" / session_id / "audit.jsonl"
            audit.parent.mkdir(parents=True)
            audit.write_text(
                json.dumps({
                    "sessionId": "audit-session",
                    "type": "user",
                    "message": {"role": "user", "content": "Audit prompt"},
                }) + "\n",
                encoding="utf-8",
            )
            with patch("pathlib.Path.home", return_value=home):
                entries = anthropic_claude_provider.index(home)

        desktop_entry = next(entry for entry in entries if entry["id"] == session_id)
        audit_entry = next(entry for entry in entries if entry["id"] == "audit-session")
        self.assertEqual(desktop_entry["_source_label"], "Mixed")
        self.assertEqual(audit_entry["_source_label"], "Desktop")

    def test_index_builds_recursive_subagent_summary_tree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            project = home / ".claude" / "projects" / "project"
            root = project / "root.jsonl"
            child = project / "root" / "subagents" / "agent-child.jsonl"
            grandchild = child.parent / child.stem / "subagents" / "agent-grandchild.jsonl"
            for path in (root, child, grandchild):
                path.parent.mkdir(parents=True, exist_ok=True)
            root.write_text("\n".join([
                json.dumps({
                    "sessionId": "root",
                    "type": "user",
                    "message": {"role": "user", "content": "Delegate work"},
                }),
                json.dumps({
                    "sessionId": "root",
                    "type": "assistant",
                    "message": {
                        "id": "parent-message",
                        "role": "assistant",
                        "content": [{"type": "tool_use", "id": "tool-child", "name": "Agent", "input": {}}],
                        "usage": {"input_tokens": 3, "output_tokens": 1},
                    },
                }),
            ]) + "\n", encoding="utf-8")
            usage = {"input_tokens": 1, "output_tokens": 2}
            for path, session_id in ((child, "root"), (grandchild, "root")):
                path.write_text("\n".join([
                    json.dumps({"type": "attachment", "attachment": {"type": "prompt_snapshot", "systemPrompt": ["Child-only context"]}}),
                    json.dumps({"sessionId": session_id, "type": "user", "message": {"role": "user", "content": "Work"}}),
                    json.dumps({"sessionId": session_id, "type": "assistant", "message": {"id": path.stem, "model": "claude-sonnet-5", "role": "assistant", "content": "Done", "usage": usage}}),
                ]) + "\n", encoding="utf-8")
            child.with_suffix(".meta.json").write_text(json.dumps({
                "description": "Review delegated work",
                "toolUseId": "tool-child",
                "model": "claude-sonnet-5",
                "spawnDepth": 1,
            }), encoding="utf-8")

            with patch("pathlib.Path.home", return_value=home):
                entries = anthropic_claude_provider.index(home)
                session = anthropic_claude_provider.details(entries[0])

        self.assertEqual(len(session["subagents"]), 1)
        self.assertEqual(len(session["subagents"][0]["subagents"]), 1)
        self.assertEqual(session["subagentTokens"]["outputTokens"], 4)
        linked_agent = session["turns"][0]["invocations"][0]["tools"][0]["subagent"]
        self.assertEqual(linked_agent["agentDescription"], "Review delegated work")
        self.assertEqual(linked_agent["ownTokens"]["outputTokens"], 2)
        self.assertEqual(session["turns"][0]["tokens"]["inputTokens"], 5)
        self.assertEqual(session["turns"][0]["tokens"]["outputTokens"], 5)
        self.assertEqual(session["tokens"]["inputTokens"], 5)
        self.assertEqual(session["tokens"]["outputTokens"], 5)
        self.assertEqual(
            linked_agent["turns"][0]["internalInstructions"][0]["name"],
            "Claude prompt_snapshot",
        )
        self.assertIn("Child-only context", linked_agent["turns"][0]["internalInstructions"][0]["content"])
        self.assertIsNotNone(linked_agent["costUsd"])
        self.assertNotIn("internalInstructions", session["turns"][0])
        totals, _, agent_count = session_token_viewer.invocation_rollup(
            session["turns"][0]["invocations"][0]
        )
        self.assertEqual(agent_count, 2)
        self.assertEqual(totals["inputTokens"], 5)
        self.assertEqual(totals["outputTokens"], 5)

    def test_pre_turn_attachments_are_visible_as_internal_instructions(self) -> None:
        records = [
            {
                "type": "attachment",
                "attachment": {
                    "type": "prompt_snapshot",
                    "systemPrompt": ["You are an interactive software engineering agent."],
                },
            },
            {
                "type": "attachment",
                "attachment": {
                    "type": "agent_listing_delta",
                    "addedTypes": ["Explore", "Plan"],
                },
            },
            {
                "type": "user",
                "uuid": "user-1",
                "sessionId": "session-1",
                "message": {"role": "user", "content": "Inspect the project"},
            },
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session-1.jsonl"
            path.write_text("\n".join(json.dumps(record) for record in records), encoding="utf-8")
            session = anthropic_claude_provider.details({"_source": path})

        instructions = session["turns"][0]["internalInstructions"]
        self.assertEqual([item["name"] for item in instructions], [
            "Claude prompt_snapshot",
            "Claude agent_listing_delta",
        ])
        self.assertIn("interactive software engineering agent", instructions[0]["content"])
        self.assertIn("Explore", instructions[1]["content"])

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

    def test_background_task_notification_stays_in_originating_turn(self) -> None:
        records = [
            {"type": "user", "uuid": "user-1", "promptId": "turn-1", "sessionId": "session-1", "message": {"role": "user", "content": "Run an agent"}},
            {"type": "assistant", "uuid": "assistant-1", "message": {"id": "message-1", "role": "assistant", "content": [{"type": "tool_use", "id": "tool-agent", "name": "Agent", "input": {}}], "usage": {"input_tokens": 10, "output_tokens": 5}}},
            {"type": "user", "uuid": "notification-1", "promptId": "notification-turn", "message": {"role": "user", "content": "<task-notification><task-id>agent-1</task-id><tool-use-id>tool-agent</tool-use-id><status>completed</status><result>Delegated result</result></task-notification>"}},
            {"type": "assistant", "uuid": "assistant-2", "message": {"id": "message-2", "role": "assistant", "content": "The agent finished.", "usage": {"input_tokens": 12, "output_tokens": 6}}},
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session-1.jsonl"
            path.write_text("\n".join(json.dumps(record) for record in records), encoding="utf-8")
            session = anthropic_claude_provider.details({"_source": path})

        self.assertEqual(len(session["turns"]), 1)
        turn = session["turns"][0]
        self.assertEqual(turn["user"], "Run an agent")
        self.assertEqual(len(turn["invocations"]), 2)
        self.assertEqual(turn["tools"][0]["result"], "Delegated result")
        self.assertEqual(turn["assistant"][-1], "The agent finished.")

    def test_final_duplicate_usage_replaces_partial_usage(self) -> None:
        records = [
            {
                "type": "user",
                "uuid": "user-1",
                "sessionId": "session-1",
                "message": {"role": "user", "content": "Write a haiku"},
            },
            {
                "type": "assistant",
                "uuid": "assistant-partial",
                "message": {
                    "id": "message-1",
                    "role": "assistant",
                    "content": "A partial response",
                    "usage": {"input_tokens": 10, "output_tokens": 4},
                },
            },
            {
                "type": "assistant",
                "uuid": "assistant-final",
                "message": {
                    "id": "message-1",
                    "role": "assistant",
                    "content": "A final response",
                    "stop_reason": "end_turn",
                    "usage": {
                        "input_tokens": 10,
                        "output_tokens": 273,
                        "output_tokens_details": {"thinking_tokens": 249},
                    },
                },
            },
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session-1.jsonl"
            path.write_text("\n".join(json.dumps(record) for record in records), encoding="utf-8")
            session = anthropic_claude_provider.details({"_source": path})

        self.assertEqual(session["tokens"]["outputTokens"], 273)
        self.assertEqual(session["tokens"]["reasoningTokens"], 249)
        self.assertEqual(session["turns"][0]["invocations"][0]["tokens"]["outputTokens"], 273)

    def test_synthetic_error_does_not_replace_or_inherit_real_model_for_pricing(self) -> None:
        records = [
            {"type": "user", "uuid": "user-1", "sessionId": "session-1", "message": {"role": "user", "content": "Run the task"}},
            {"type": "assistant", "uuid": "assistant-1", "message": {"id": "message-1", "model": "claude-sonnet-4-5", "role": "assistant", "content": "Working", "usage": {"input_tokens": 10, "output_tokens": 5}}},
            {"type": "assistant", "uuid": "error-1", "isApiErrorMessage": True, "error": "server_error", "message": {"id": "error-message", "model": "<synthetic>", "role": "assistant", "content": "API Error", "usage": {"input_tokens": 0, "output_tokens": 0}}},
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session-1.jsonl"
            path.write_text("\n".join(json.dumps(record) for record in records), encoding="utf-8")
            session = pricing.apply_costs(anthropic_claude_provider.details({"_source": path}))

        turn = session["turns"][0]
        self.assertEqual(turn["model"], "claude-sonnet-4-5")
        self.assertIsNotNone(turn["costUsd"])
        self.assertEqual(turn["invocations"][0]["model"], "claude-sonnet-4-5")
        self.assertIsNotNone(turn["invocations"][0]["costUsd"])
        self.assertEqual(turn["invocations"][1]["model"], "<synthetic>")
        self.assertEqual(turn["invocations"][1]["costUsd"], 0.0)


if __name__ == "__main__":
    unittest.main()
