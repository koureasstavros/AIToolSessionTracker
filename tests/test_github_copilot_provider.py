import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from src.providers import github_copilot_local_provider


class CopilotInvocationGroupingTests(unittest.TestCase):
    def test_session_state_client_name_identifies_desktop(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory) / "session-1"
            folder.mkdir()
            (folder / "workspace.yaml").write_text(
                "client_name: github/autopilot\n",
                encoding="utf-8",
            )
            self.assertEqual(
                github_copilot_local_provider._surface_from_session_state(folder),
                "Desktop",
            )

    def test_shared_cli_desktop_sources_are_mixed(self) -> None:
        self.assertEqual(
            github_copilot_local_provider.tool({"_kind": "copilot-session-state"}),
            "Mixed",
        )
        self.assertEqual(
            github_copilot_local_provider.tool({"_kind": "copilot-db"}),
            "Mixed",
        )
        self.assertEqual(
            github_copilot_local_provider.tool({"_kind": "copilot-chat"}),
            "Extension",
        )

    def test_delete_removes_all_merged_session_sources(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "session-1"
            state.mkdir()
            (state / "events.jsonl").write_text("", encoding="utf-8")
            database = root / "session-store.db"
            with closing(sqlite3.connect(database)) as db:
                db.execute("CREATE TABLE sessions (id TEXT, summary TEXT, updated_at TEXT)")
                db.execute("CREATE TABLE turns (session_id TEXT, content TEXT)")
                db.execute("INSERT INTO sessions VALUES ('session-1', 'Merged session', '')")
                db.execute("INSERT INTO turns VALUES ('session-1', 'content')")
                db.commit()
            summary = {
                "id": "session-1",
                "_kind": "copilot-session-state",
                "_source": state,
                "_sources": [
                    {"id": "session-1", "_kind": "copilot-session-state", "_source": state},
                    {"id": "session-1", "_kind": "copilot-db", "_source": database, "_session_id": "session-1"},
                ],
            }

            github_copilot_local_provider.delete(summary)

            self.assertFalse(state.exists())
            with closing(sqlite3.connect(database)) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM sessions WHERE id = 'session-1'").fetchone()[0], 0)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM turns WHERE session_id = 'session-1'").fetchone()[0], 0)

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
            session = github_copilot_local_provider._read_session_state(folder)

        self.assertEqual(len(session["turns"]), 1)
        turn = session["turns"][0]
        self.assertEqual(len(turn["invocations"]), 2)
        self.assertEqual([len(invocation["tools"]) for invocation in turn["invocations"]], [1, 0])
        self.assertEqual(turn["invocations"][0]["tools"][0]["name"], "read_file")
        self.assertEqual(turn["invocations"][0]["tools"][0]["result"], "# Project")
        self.assertEqual([invocation["tokens"]["outputTokens"] for invocation in turn["invocations"]], [10, 5])

    def test_session_state_links_usage_records_to_assistant_api_calls(self) -> None:
        records = [
            {"type": "user.message", "data": {"interactionId": "turn-a", "content": "First"}},
            {"type": "assistant.message", "data": {"interactionId": "turn-a", "apiCallId": "call-a", "content": "Part one"}},
            {"type": "assistant.message", "data": {"interactionId": "turn-a", "apiCallId": "call-a", "content": "Part two"}},
            {"type": "session.usage_record", "data": {"usage": {"apiCallId": "call-a", "inputTokens": 100, "cacheReadTokens": 10, "cacheWriteTokens": 20, "outputTokens": 7, "reasoningTokens": 3}}},
            {"type": "session.usage_record", "data": {"usage": {"apiCallId": "call-a", "inputTokens": 100, "cacheReadTokens": 10, "cacheWriteTokens": 20, "outputTokens": 7, "reasoningTokens": 3}}},
            {"type": "session.usage_record", "data": {"usage": {"apiCallId": "call-b", "inputTokens": 200, "cacheReadTokens": 0, "cacheWriteTokens": 0, "outputTokens": 0, "reasoningTokens": 0}}},
            {"type": "user.message", "data": {"interactionId": "turn-b", "content": "Second"}},
            {"type": "assistant.message", "data": {"interactionId": "turn-b", "apiCallId": "call-b", "content": "Done"}},
            {"type": "user.message", "data": {"interactionId": "turn-c", "content": "Third"}},
            {"type": "assistant.message", "data": {"interactionId": "turn-c", "apiCallId": "call-c", "content": "Done", "inputTokens": 9, "outputTokens": 5}},
            {"type": "session.usage_record", "data": {"usage": {"apiCallId": "call-c", "inputTokens": 300, "cacheReadTokens": 1, "cacheWriteTokens": 2, "outputTokens": 5, "reasoningTokens": 4}}},
            {"type": "user.message", "data": {"interactionId": "turn-d", "content": "No usage yet"}},
            {"type": "assistant.message", "data": {"interactionId": "turn-d", "apiCallId": "call-d", "content": "Pending"}},
            {"type": "session.usage_record", "data": {"usage": {"apiCallId": "unmatched", "inputTokens": 400, "cacheReadTokens": 5, "cacheWriteTokens": 6, "outputTokens": 40, "reasoningTokens": 7}}},
            {"type": "session.shutdown", "data": {"modelMetrics": {"model": {"usage": {"inputTokens": 999, "cacheReadTokens": 3, "cacheWriteTokens": 4, "outputTokens": 99, "reasoningTokens": 11}}}}},
        ]
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory) / "session-1"
            folder.mkdir()
            (folder / "events.jsonl").write_text(
                "\n".join(json.dumps(record) for record in records), encoding="utf-8"
            )
            session = github_copilot_local_provider._read_session_state(folder)

        expected = {
            "inputTokens": [100, 200, 9, None],
            "cacheReadTokens": [10, 0, 1, None],
            "cacheWriteTokens": [20, 0, 2, None],
            "outputTokens": [7, 0, 5, None],
            "reasoningTokens": [3, 0, 4, None],
        }
        for key, values in expected.items():
            self.assertEqual([turn["tokens"][key] for turn in session["turns"]], values)
            self.assertEqual(session["turns"][0]["invocations"][0]["tokens"][key], values[0])
            self.assertIsNone(session["turns"][0]["invocations"][1]["tokens"][key])
        self.assertEqual(session["tokens"]["inputTokens"], 999)
        self.assertEqual(session["tokens"]["outputTokens"], 99)
        self.assertNotIn("estimated", session.get("tokenFlags", []))

    def test_session_state_without_usage_records_keeps_estimated_turn_input(self) -> None:
        records = [
            {"type": "user.message", "data": {"interactionId": "turn-a", "content": "First"}},
            {"type": "assistant.message", "data": {"interactionId": "turn-a", "outputTokens": 2}},
            {"type": "user.message", "data": {"interactionId": "turn-b", "content": "Second"}},
            {"type": "assistant.message", "data": {"interactionId": "turn-b", "outputTokens": 8}},
            {"type": "session.shutdown", "data": {"modelMetrics": {"model": {"usage": {"inputTokens": 100, "reasoningTokens": 10}}}}},
        ]
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory) / "session-1"
            folder.mkdir()
            (folder / "events.jsonl").write_text(
                "\n".join(json.dumps(record) for record in records), encoding="utf-8"
            )
            session = github_copilot_local_provider._read_session_state(folder)

        self.assertEqual([turn["tokens"]["inputTokens"] for turn in session["turns"]], [20, 80])
        self.assertEqual([turn["tokens"]["reasoningTokens"] for turn in session["turns"]], [2, 8])
        self.assertIn("estimated", session["tokenFlags"])

    def test_session_state_captures_reasoning_effort(self) -> None:
        records = [
            {"type": "session.start", "data": {"selectedModel": "gpt-test", "reasoningEffort": "high"}},
            {"type": "user.message", "data": {"interactionId": "turn-1", "content": "Analyze this"}},
            {"type": "assistant.message", "data": {"interactionId": "turn-1", "content": "Done", "reasoningEffort": "medium"}},
        ]
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory) / "session-1"
            folder.mkdir()
            (folder / "events.jsonl").write_text(
                "\n".join(json.dumps(record) for record in records),
                encoding="utf-8",
            )
            session = github_copilot_local_provider._read_session_state(folder)

        self.assertEqual(session["reasoningEffort"], "high")
        self.assertEqual(session["turns"][0]["reasoningEffort"], "medium")
        self.assertEqual(session["turns"][0]["invocations"][0]["reasoningEffort"], "medium")

    def test_session_state_captures_nested_checkpoint_effort(self) -> None:
        records = [{
            "type": "session.usage_checkpoint",
            "data": {"promptCacheBreakState": [{
                "models": {"gpt-test": {"model": "gpt-test", "reasoning_effort": "medium"}},
            }]},
        }]
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory) / "session-1"
            folder.mkdir()
            (folder / "events.jsonl").write_text(
                "\n".join(json.dumps(record) for record in records),
                encoding="utf-8",
            )
            session = github_copilot_local_provider._read_session_state(folder)

        self.assertEqual(session["reasoningEffort"], "medium")

    def test_extension_chat_captures_selected_model_effort(self) -> None:
        records = [
            {"v": {"sessionId": "session-1", "requests": [{
                "requestId": "request-1",
                "message": {"text": "Analyze this"},
                "response": [{"value": "Done"}],
            }], "inputState": {"selectedModel": {
                "identifier": "gpt-test",
                "modelConfiguration": {"reasoningEffort": "medium"},
            }}}},
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session-1.jsonl"
            path.write_text("\n".join(json.dumps(record) for record in records), encoding="utf-8")
            session = github_copilot_local_provider._read_chat(path)

        self.assertEqual(session["reasoningEffort"], "medium")

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
            session = github_copilot_local_provider._read_chat(path)

        self.assertEqual(len(session["turns"]), 1)
        turn = session["turns"][0]
        self.assertEqual(turn["tokens"]["inputTokens"], 300)
        self.assertEqual(turn["tokens"]["outputTokens"], 70)
        self.assertEqual(len(turn["invocations"]), 2)
        self.assertEqual([len(invocation["tools"]) for invocation in turn["invocations"]], [1, 0])
        self.assertEqual(turn["invocations"][1]["tokens"]["inputTokens"], 250)
        self.assertEqual(turn["invocations"][1]["tokens"]["outputTokens"], 20)

    def test_chat_run_subagent_is_nested_with_unavailable_usage(self) -> None:
        call_id = "call-agent"
        request = {
            "requestId": "request-1",
            "modelId": "gpt-parent",
            "message": {"text": "Delegate work"},
            "promptTokens": 100,
            "completionTokens": 20,
            "response": [],
            "result": {
                "metadata": {
                    "promptTokens": 80,
                    "outputTokens": 5,
                    "toolCallRounds": [
                        {
                            "modelId": "gpt-child",
                            "reasoningEffort": "high",
                            "toolCalls": [{
                                "id": call_id,
                                "name": "runSubagent",
                                "arguments": json.dumps({
                                    "description": "Review code",
                                    "prompt": "Review this code",
                                    "agentName": "Reviewer",
                                }),
                            }],
                        },
                        {"modelId": "gpt-parent", "toolCalls": [], "response": "Done"},
                    ],
                    "toolCallResults": {
                        call_id: {"content": [{"value": "Looks good"}]},
                    },
                },
            },
        }
        document = {"v": {"sessionId": "session-1", "requests": [request]}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session-1.jsonl"
            path.write_text(json.dumps(document), encoding="utf-8")
            session = github_copilot_local_provider.details({
                "_source": path,
                "_kind": "copilot-chat",
            })

        self.assertEqual(len(session["subagents"]), 1)
        agent = session["turns"][0]["invocations"][0]["tools"][0]["subagent"]
        self.assertEqual(agent["name"], "Reviewer")
        self.assertEqual(agent["agentDescription"], "Review code")
        self.assertEqual(agent["reasoningEffort"], "high")
        self.assertEqual(agent["turns"][0]["assistant"], ["Looks good"])
        self.assertTrue(all(value is None for value in agent["tokens"].values()))

    def test_database_subagent_usage_is_nested_under_owning_task(self) -> None:
        parent_interaction = "parent-interaction"
        child_interaction = "child-interaction"
        tool_call_id = "call-agent"
        prompt = "Return one delegated result"
        records = [
            {"type": "user.message", "data": {"interactionId": parent_interaction, "content": "Delegate work"}},
            {"type": "system.message", "data": {"interactionId": parent_interaction, "content": "Parent-only instructions"}},
            {"type": "assistant.turn_start", "data": {"interactionId": parent_interaction, "turnId": "0"}},
            {"type": "assistant.message", "data": {"interactionId": parent_interaction, "turnId": "0", "content": "Delegating"}},
            {"type": "tool.execution_start", "data": {"turnId": "0", "toolCallId": tool_call_id, "toolName": "task", "arguments": {"name": "Worker", "description": "Do work", "prompt": prompt}}},
            {"type": "subagent.started", "data": {"toolCallId": tool_call_id, "agentDisplayName": "Worker", "agentDescription": "Do work", "model": "gpt-test"}},
            {"type": "user.message", "data": {"interactionId": child_interaction, "content": prompt}},
            {"type": "system.message", "data": {"interactionId": child_interaction, "content": "Child-only instructions"}},
            {"type": "assistant.turn_start", "data": {"interactionId": child_interaction, "turnId": "0"}},
            {"type": "assistant.message", "data": {"interactionId": child_interaction, "turnId": "0", "content": "Child result"}},
            {"type": "subagent.completed", "data": {"toolCallId": tool_call_id, "agentDisplayName": "Worker", "totalTokens": 30}},
            {"type": "tool.execution_complete", "data": {"interactionId": parent_interaction, "turnId": "0", "toolCallId": tool_call_id, "success": True, "result": "Child result"}},
            {"type": "assistant.turn_start", "data": {"interactionId": parent_interaction, "turnId": "1"}},
            {"type": "assistant.message", "data": {"interactionId": parent_interaction, "turnId": "1", "content": "Complete"}},
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "session-1"
            state.mkdir()
            (state / "events.jsonl").write_text(
                "\n".join(json.dumps(record) for record in records),
                encoding="utf-8",
            )
            database = root / "session-store.db"
            with closing(sqlite3.connect(database)) as db:
                db.execute("CREATE TABLE sessions (id TEXT, summary TEXT, updated_at TEXT)")
                db.execute("CREATE TABLE turns (session_id TEXT, turn_index INTEGER, user_message TEXT, assistant_response TEXT)")
                db.execute("CREATE TABLE assistant_usage_events (id INTEGER PRIMARY KEY, session_id TEXT, turn_index INTEGER, agent_id TEXT, parent_tool_call_id TEXT, model TEXT, input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER, cache_write_tokens INTEGER, reasoning_tokens INTEGER, initiator TEXT, created_at TEXT)")
                db.execute("INSERT INTO sessions VALUES (?, ?, ?)", ("session-1", "Delegation", "2026-09-10T00:00:00Z"))
                rows = [
                    (1, "session-1", 0, None, None, "gpt-5.6-luna", 20, 4, 0, 18, 1, "user", "2026-09-10T00:00:01Z"),
                    (2, "session-1", 0, "agent-1", tool_call_id, "gpt-5.6-luna", 30, 5, 20, 7, 2, "sub-agent", "2026-09-10T00:00:02Z"),
                    (3, "session-1", 0, None, None, "gpt-5.6-luna", 25, 3, 18, 5, 0, "agent", "2026-09-10T00:00:03Z"),
                ]
                db.executemany("INSERT INTO assistant_usage_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
                db.commit()
            summary = {
                "id": "session-1",
                "name": "Delegation",
                "updated": 0,
                "_source": state,
                "_kind": "copilot-session-state",
                "_sources": [
                    {"id": "session-1", "_source": state, "_kind": "copilot-session-state"},
                    {"id": "session-1", "_source": database, "_kind": "copilot-db", "_session_id": "session-1"},
                ],
            }
            session = github_copilot_local_provider.details(summary)

        self.assertEqual(len(session["turns"]), 1)
        self.assertEqual(len(session["subagents"]), 1)
        self.assertEqual(len(session["turns"][0]["invocations"]), 2)
        tool = session["turns"][0]["invocations"][0]["tools"][0]
        self.assertEqual(tool["subagent"]["name"], "Worker")
        self.assertEqual(tool["subagent"]["tokens"]["inputTokens"], 3)
        self.assertEqual(tool["subagent"]["tokens"]["outputTokens"], 5)
        self.assertEqual(
            session["turns"][0]["internalInstructions"][0]["content"],
            "Parent-only instructions",
        )
        self.assertEqual(
            tool["subagent"]["turns"][0]["internalInstructions"][0]["content"],
            "Child-only instructions",
        )
        self.assertIsNotNone(tool["subagent"]["costUsd"])
        self.assertEqual(
            session["turns"][0]["invocations"][1]["assistant"],
            ["Complete"],
        )
        self.assertNotIn("estimated", session.get("tokenFlags", []))
        self.assertEqual(
            set(session["turns"][0]["tokenFields"]),
            {"inputTokens", "cacheReadTokens", "cacheWriteTokens", "outputTokens", "reasoningTokens"},
        )


if __name__ == "__main__":
    unittest.main()
