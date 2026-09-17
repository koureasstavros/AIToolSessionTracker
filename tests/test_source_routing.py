import json
import socket
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from session_token_viewer import OtelReceiver, PROVIDERS, load_session_index, render_settings_page
from src.common import source_otel, source_routing
from src.common.tracker_database import (
    config_database_path,
    connect_database,
    content_database_path,
)
from src.providers import github_copilot_otel_provider


class SourceRoutingTests(unittest.TestCase):
    @staticmethod
    def _protobuf_varint(value: int) -> bytes:
        encoded = bytearray()
        while value > 0x7F:
            encoded.append((value & 0x7F) | 0x80)
            value >>= 7
        encoded.append(value)
        return bytes(encoded)

    @classmethod
    def _protobuf_bytes(cls, field: int, value: bytes) -> bytes:
        return cls._protobuf_varint((field << 3) | 2) + cls._protobuf_varint(len(value)) + value

    @classmethod
    def _protobuf_text(cls, field: int, value: str) -> bytes:
        return cls._protobuf_bytes(field, value.encode("utf-8"))

    @classmethod
    def _protobuf_fixed64(cls, field: int, value: int) -> bytes:
        return cls._protobuf_varint((field << 3) | 1) + value.to_bytes(8, "little")

    @classmethod
    def _protobuf_attribute(cls, key: str, value: str) -> bytes:
        any_value = cls._protobuf_text(1, value)
        return cls._protobuf_text(1, key) + cls._protobuf_bytes(2, any_value)

    def test_defaults_route_every_provider_to_local_storage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "tracker.db"
            routing = source_routing.load_source_routing(PROVIDERS, database_path)
            with closing(sqlite3.connect(database_path)) as database:
                seeded = database.execute(
                    "SELECT value FROM tracker_metadata WHERE key = 'source_routing_json_seeded'"
                ).fetchone()[0]

        self.assertEqual(routing["providers"], {provider: "local" for provider in PROVIDERS})
        self.assertEqual(seeded, "1")
        self.assertEqual(source_otel.load_source_otel(database_path)["protocol"], "otlp_http")

    def test_default_configuration_and_content_database_paths_are_separate(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch(
            "src.common.tracker_database.Path.cwd", return_value=Path(directory)
        ):
            config_path = config_database_path()
            content_path = content_database_path()

        self.assertEqual(config_path.name, "AI-Tool-Session-Tracker-config.db")
        self.assertEqual(content_path.name, "AI-Tool-Session-Tracker-content.db")
        self.assertNotEqual(config_path, content_path)

    def test_source_routing_adds_missing_bundled_entries_on_later_load(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "AI-Tool-Session-Tracker-config.db"
            source_routing.load_source_routing(PROVIDERS, database_path)
            with closing(sqlite3.connect(database_path)) as database:
                database.execute("DELETE FROM source_routing WHERE provider = 'claude'")
                database.commit()

            routing = source_routing.load_source_routing(PROVIDERS, database_path)

            self.assertEqual(routing["providers"]["claude"], "local")

    def test_nested_context_is_not_part_of_user_request(self) -> None:
        text = (
            "<context>Current environment</context><userRequest>"
            "Answer briefly\n<context>Earlier prompt</context><userRequest>Old request</userRequest>"
            "</userRequest>"
        )

        request, context = github_copilot_otel_provider.split_user_request(text)

        self.assertEqual(request, "Answer briefly")
        self.assertIn("Current environment", context[0]["content"])
        self.assertIn("Earlier prompt", context[0]["content"])

    def test_incomplete_nested_context_is_not_part_of_user_request(self) -> None:
        text = (
            "<context>Current environment</context><userRequest>Answer briefly\nQuoted earlier request\n"
            "<context>Earlier prompt</context><userRequest>Old request</userRequest>"
        )

        request, context = github_copilot_otel_provider.split_user_request(text)

        self.assertEqual(request, "Answer briefly")
        self.assertIn("Quoted earlier request", context[0]["content"])
        self.assertIn("Earlier prompt", context[0]["content"])

    def test_routes_round_trip_and_validate_listener(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "routing.json"
            routes = {provider: "local" for provider in PROVIDERS}
            routes["claude"] = "otel"

            source_routing.save_source_routing(PROVIDERS, routes, path)

            self.assertEqual(source_routing.load_source_routing(PROVIDERS, path)["providers"]["claude"], "otel")
            with closing(sqlite3.connect(path)) as connection:
                stored = connection.execute("SELECT provider, source FROM source_routing").fetchall()
            self.assertNotIn("secret", repr(stored).lower())
            source_otel.save_source_otel("otlp_http", "127.0.0.1", "4318", path)
            self.assertEqual(source_otel.load_source_otel(path)["port"], 4318)
            with self.assertRaises(ValueError):
                source_otel.save_source_otel("otlp_http", "127.0.0.1", "0", path)

    def test_local_only_providers_expose_only_local_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "routing.db"
            configuration = source_routing.load_source_routing(PROVIDERS, path)

            self.assertEqual(configuration["provider_options"]["antigravity"], ["local"])
            self.assertEqual(configuration["provider_options"]["xai_cursor"], ["local"])
            self.assertEqual(configuration["provider_options"]["cognition_devin"], ["local"])
            self.assertEqual(configuration["provider_options"]["m365_copilot"], ["local"])
            routes = {provider: "local" for provider in PROVIDERS}
            routes["cognition_devin"] = "otel"
            with self.assertRaises(ValueError):
                source_routing.save_source_routing(PROVIDERS, routes, path)

    def test_source_otel_seeds_from_bundled_catalog_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tracker.db"
            otel = source_otel.load_source_otel(path)
            with closing(sqlite3.connect(path)) as database:
                stored = database.execute(
                    "SELECT protocol, host, port FROM source_otel WHERE id = 1"
                ).fetchone()

        self.assertEqual(otel, {"protocol": "otlp_http", "host": "127.0.0.1", "port": 4318})
        self.assertEqual(stored, ("otlp_http", "127.0.0.1", 4318))

    def test_otlp_spans_are_persisted_and_read_as_provider_sessions(self) -> None:
        payload = {
            "resourceSpans": [{
                "resource": {"attributes": [{"key": "ai.session.provider", "value": {"stringValue": "copilot"}}]},
                "scopeSpans": [{"spans": [{
                    "traceId": "trace-1", "spanId": "span-1", "name": "chat", "startTimeUnixNano": "1000000000", "endTimeUnixNano": "2000000000",
                    "attributes": [
                        {"key": "ai.session.id", "value": {"stringValue": "session-1"}},
                        {"key": "copilot_chat.user_request", "value": {"stringValue": "Analyze the project metadata"}},
                        {"key": "copilot_chat.repo.remote_url", "value": {"stringValue": "https://github.com/example/project"}},
                        {"key": "gen_ai.request.model", "value": {"stringValue": "claude-test"}},
                        {"key": "gen_ai.usage.input_tokens", "value": {"intValue": "12"}},
                        {"key": "gen_ai.usage.cache_read.input_tokens", "value": {"intValue": "9"}},
                        {"key": "gen_ai.usage.output_tokens", "value": {"intValue": "7"}},
                        {"key": "gen_ai.usage.reasoning.output_tokens", "value": {"intValue": "2"}},
                        {"key": "gen_ai.input.messages", "value": {"stringValue": json.dumps([
                            {"role": "user", "parts": [{"type": "text", "content": "Inspect the project"}]},
                        ])}},
                        {"key": "gen_ai.output.messages", "value": {"stringValue": json.dumps([
                            {"role": "assistant", "parts": [
                                {"type": "text", "content": "I will inspect it."},
                                {"type": "tool_call", "id": "call-1", "name": "runSubagent", "arguments": json.dumps({"agentName": "Reviewer", "description": "Review files"})},
                            ]},
                        ])}},
                    ],
                }]}],
            }],
        }
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "otel.db"
            self.assertEqual(source_otel.ingest_otlp_json(payload, PROVIDERS, database), 1)
            summary = source_otel.index("copilot", database)[0]
            details = source_otel.details(summary, "copilot", database)

        self.assertEqual(summary["_route"], "otel")
        self.assertEqual(summary["name"], "Analyze the project metadata")
        self.assertEqual(summary["project"], "https://github.com/example/project")
        self.assertEqual(details["project"], "https://github.com/example/project")
        self.assertEqual(details["tokens"]["inputTokens"], 3)
        self.assertEqual(details["tokens"]["cacheReadTokens"], 9)
        self.assertEqual(details["tokens"]["outputTokens"], 5)
        self.assertEqual(details["tokens"]["reasoningTokens"], 2)
        self.assertEqual(details["turns"][0]["invocations"][0]["model"], "claude-test")
        self.assertEqual(details["turns"][0]["user"], "Inspect the project")
        self.assertEqual(details["turns"][0]["assistant"], ["I will inspect it."])
        tool = details["turns"][0]["invocations"][0]["tools"][0]
        self.assertEqual(tool["name"], "runSubagent")
        self.assertEqual(tool["subagent"]["name"], "Reviewer")

    def test_copilot_otlp_protobuf_trace_is_ingested(self) -> None:
        attributes = b"".join([
            self._protobuf_bytes(9, self._protobuf_attribute("gen_ai.provider.name", "github")),
            self._protobuf_bytes(9, self._protobuf_attribute("gen_ai.conversation.id", "copilot-session-1")),
            self._protobuf_bytes(9, self._protobuf_attribute("gen_ai.input.messages", '[{"role":"user","parts":[{"type":"text","content":"Hello"}]}]')),
            self._protobuf_bytes(9, self._protobuf_attribute("gen_ai.request.model", "gpt-5")),
            self._protobuf_bytes(9, self._protobuf_attribute("gen_ai.usage.input_tokens", "42")),
            self._protobuf_bytes(9, self._protobuf_attribute("gen_ai.usage.output_tokens", "8")),
        ])
        span = b"".join([
            self._protobuf_bytes(1, b"\x01" * 16),
            self._protobuf_bytes(2, b"\x02" * 8),
            self._protobuf_text(5, "chat"),
            self._protobuf_fixed64(7, 1_000_000_000),
            self._protobuf_fixed64(8, 2_000_000_000),
            attributes,
        ])
        scope_spans = self._protobuf_bytes(2, span)
        resource_spans = self._protobuf_bytes(2, scope_spans)
        request = self._protobuf_bytes(1, resource_spans)
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "tracker.db"
            payload = source_otel.otlp_protobuf_to_json(request)
            self.assertEqual(source_otel.ingest_otlp_json(payload, PROVIDERS, database), 1)
            summary = source_otel.index("copilot", database)[0]
            details = source_otel.details(summary, "copilot", database)

        self.assertEqual(summary["id"], "copilot-session-1")
        self.assertEqual(details["model"], "gpt-5")
        self.assertEqual(details["tokens"]["inputTokens"], 42)
        self.assertEqual(details["tokens"]["outputTokens"], 8)

    def test_otlp_route_uses_local_otel_database_not_adapter(self) -> None:
        configuration = {"providers": {provider: "local" for provider in PROVIDERS}, "otel": {}}
        configuration["providers"]["copilot"] = "otel"
        with patch("session_token_viewer.source_routing_config", return_value=configuration), patch(
            "session_token_viewer.source_otel.index", return_value=[{"id": "otel-1", "_kind": "otel", "_route": "otel", "_has_data": True}]
        ) as index:
            summaries = load_session_index(Path("."), "copilot")

        index.assert_called_once_with("copilot")
        self.assertEqual(summaries[0]["id"], "otel-1")

    def test_delete_removes_all_otel_spans_for_session(self) -> None:
        payload = {
            "resourceSpans": [{
                "resource": {"attributes": [{"key": "ai.session.provider", "value": {"stringValue": "copilot"}}]},
                "scopeSpans": [{"spans": [
                    {"traceId": "trace-1", "spanId": "span-1", "name": "chat", "startTimeUnixNano": "1", "endTimeUnixNano": "2", "attributes": [{"key": "ai.session.id", "value": {"stringValue": "session-1"}}, {"key": "gen_ai.input.messages", "value": {"stringValue": "[{\"role\":\"user\",\"parts\":[{\"type\":\"text\",\"content\":\"Delete me\"}]}]"}}]},
                    {"traceId": "trace-1", "spanId": "span-2", "name": "chat", "startTimeUnixNano": "2", "endTimeUnixNano": "3", "attributes": [{"key": "ai.session.id", "value": {"stringValue": "session-1"}}]},
                ]}],
            }],
        }
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "tracker.db"
            source_otel.ingest_otlp_json(payload, PROVIDERS, database)
            summary = source_otel.index("copilot", database)[0]
            self.assertTrue(source_otel.delete(summary, "copilot", database))
            self.assertEqual(source_otel.index("copilot", database), [])

    def test_tracker_database_uses_wal_for_otlp_and_viewer_access(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "tracker.db"
            with connect_database(database) as connection:
                self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")

    def test_otlp_logs_and_metrics_are_persisted(self) -> None:
        resource = {"attributes": [
            {"key": "ai.session.provider", "value": {"stringValue": "codex"}},
            {"key": "ai.session.id", "value": {"stringValue": "session-1"}},
        ]}
        logs = {"resourceLogs": [{"resource": resource, "scopeLogs": [{"logRecords": [{
            "timeUnixNano": "10", "severityText": "INFO", "body": {"stringValue": "User prompt"},
            "attributes": [{"key": "event.name", "value": {"stringValue": "codex.user_prompt"}}, {"key": "prompt", "value": {"stringValue": "User prompt"}}],
        }]}]}]}
        metrics = {"resourceMetrics": [{"resource": resource, "scopeMetrics": [{"metrics": [{
            "name": "gen_ai.client.token.usage", "sum": {"dataPoints": [{"timeUnixNano": "11"}]},
        }]}]}]}
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "tracker.db"
            self.assertEqual(source_otel.ingest_otlp_logs_json(logs, PROVIDERS, database), 1)
            self.assertEqual(source_otel.ingest_otlp_metrics_json(metrics, PROVIDERS, database), 1)
            with closing(sqlite3.connect(database)) as connection:
                log = connection.execute("SELECT provider, session_id, body FROM otel_logs").fetchone()
                metric = connection.execute("SELECT provider, session_id, name FROM otel_metrics").fetchone()
            summary = source_otel.index("codex", database)[0]
            mapped_details = source_otel.details(summary, "codex", database)

        self.assertEqual(log, ("codex", "session-1", "User prompt"))
        self.assertEqual(metric, ("codex", "session-1", "gen_ai.client.token.usage"))
        self.assertEqual(summary["name"], "User prompt")
        self.assertEqual(mapped_details["turns"][0]["user"], "User prompt")

    def test_infrastructure_only_spans_are_hidden_from_sidebar(self) -> None:
        payload = {"resourceSpans": [{
            "resource": {"attributes": [{"key": "ai.session.provider", "value": {"stringValue": "codex"}}]},
            "scopeSpans": [{"spans": [{
                "traceId": "trace-1", "spanId": "span-1", "name": "turn_context.build",
                "startTimeUnixNano": "1", "endTimeUnixNano": "2",
                "attributes": [{"key": "ai.session.id", "value": {"stringValue": "internal-session"}}],
            }]}],
        }]}
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "tracker.db"
            source_otel.ingest_otlp_json(payload, PROVIDERS, database)
            self.assertEqual(source_otel.index("codex", database), [])

    def test_codex_title_generation_prompt_is_hidden(self) -> None:
        payload = {"resourceLogs": [{
            "resource": {"attributes": [
                {"key": "ai.session.provider", "value": {"stringValue": "codex"}},
                {"key": "conversation.id", "value": {"stringValue": "title-session"}},
            ]},
            "scopeLogs": [{"logRecords": [{
                "attributes": [
                    {"key": "event.name", "value": {"stringValue": "codex.user_prompt"}},
                    {"key": "prompt", "value": {"stringValue": "You are a helpful assistant. Generate a short title for a task."}},
                ],
            }]}],
        }]}
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "tracker.db"
            source_otel.ingest_otlp_logs_json(payload, PROVIDERS, database)
            self.assertEqual(source_otel.index("codex", database), [])

    def test_agent_span_groups_nested_chat_spans_as_invocations(self) -> None:
        def attribute(key: str, value: str) -> dict:
            return {"key": key, "value": {"stringValue": value}}

        parent_attributes = [
            attribute("ai.session.id", "session-1"),
            attribute("gen_ai.input.messages", json.dumps([{"role": "user", "parts": [{"type": "text", "content": "Do work"}]}])),
            attribute("gen_ai.output.messages", json.dumps([{"role": "assistant", "parts": [{"type": "text", "content": "Done"}]}])),
        ]
        child_attributes = [
            attribute("ai.session.id", "session-1"),
            attribute("gen_ai.request.model", "gpt-test"),
            attribute("gen_ai.usage.input_tokens", "30"),
            attribute("gen_ai.usage.output_tokens", "5"),
            attribute("gen_ai.output.messages", json.dumps([{"role": "assistant", "parts": [{"type": "tool_call", "id": "call-1", "name": "read_file", "arguments": "{}"}]}])),
        ]
        payload = {"resourceSpans": [{
            "resource": {"attributes": [attribute("ai.session.provider", "copilot")]},
            "scopeSpans": [{"spans": [
                {"traceId": "trace-1", "spanId": "parent", "name": "invoke_agent Copilot", "startTimeUnixNano": "100", "endTimeUnixNano": "300", "attributes": parent_attributes},
                {"traceId": "trace-1", "spanId": "child", "name": "chat model", "startTimeUnixNano": "120", "endTimeUnixNano": "200", "attributes": child_attributes},
            ]}],
        }]}
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "tracker.db"
            source_otel.ingest_otlp_json(payload, PROVIDERS, database)
            details = source_otel.details(source_otel.index("copilot", database)[0], "copilot", database)

        self.assertEqual(len(details["turns"]), 1)
        turn = details["turns"][0]
        self.assertEqual(turn["user"], "Do work")
        self.assertEqual(turn["assistant"], ["Done"])
        self.assertEqual(len(turn["invocations"]), 1)
        self.assertEqual(turn["invocations"][0]["tools"][0]["name"], "read_file")
        self.assertEqual(turn["tools"][0]["name"], "read_file")
        self.assertEqual(turn["tokens"]["inputTokens"], 30)

    def test_orphaned_chat_spans_group_after_user_message(self) -> None:
        def attribute(key: str, value: str) -> dict:
            return {"key": key, "value": {"stringValue": value}}

        user_messages = json.dumps([{"role": "user", "parts": [{"type": "text", "content": "<context>Workspace details</context><userRequest>Continue work</userRequest>"}]}])
        payload = {"resourceSpans": [{
            "resource": {"attributes": [attribute("ai.session.provider", "copilot")]},
            "scopeSpans": [{"spans": [
                {"traceId": "trace-1", "spanId": "one", "name": "chat", "startTimeUnixNano": "100", "endTimeUnixNano": "150", "attributes": [attribute("ai.session.id", "session-1"), attribute("gen_ai.input.messages", user_messages)]},
                {"traceId": "trace-1", "spanId": "two", "name": "chat", "startTimeUnixNano": "160", "endTimeUnixNano": "200", "attributes": [attribute("ai.session.id", "session-1"), attribute("gen_ai.usage.output_tokens", "5")]},
            ]}],
        }]}
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "tracker.db"
            source_otel.ingest_otlp_json(payload, PROVIDERS, database)
            details = source_otel.details(source_otel.index("copilot", database)[0], "copilot", database)

        self.assertEqual(len(details["turns"]), 1)
        self.assertEqual(details["turns"][0]["user"], "Continue work")
        self.assertEqual(details["turns"][0]["internalInstructions"], [{"name": "Additional context", "content": "<context>Workspace details</context>"}])
        self.assertEqual(len(details["turns"][0]["invocations"]), 1)

    def test_chunked_otlp_http_request_is_ingested(self) -> None:
        payload = json.dumps({"resourceSpans": [{
            "resource": {"attributes": [{"key": "ai.session.provider", "value": {"stringValue": "copilot"}}]},
            "scopeSpans": [{"spans": [{
                "traceId": "chunked-trace", "spanId": "chunked-span", "name": "chat",
                "startTimeUnixNano": "1000000000", "endTimeUnixNano": "2000000000",
                "attributes": [{"key": "gen_ai.input.messages", "value": {"stringValue": "[{\"role\":\"user\",\"parts\":[{\"type\":\"text\",\"content\":\"Chunked\"}]}]"}}],
            }]}],
        }]}).encode("utf-8")
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "tracker.db"
            with patch("src.common.source_otel.content_database_path", return_value=database):
                receiver = OtelReceiver("127.0.0.1", 0)
                receiver.start()
                try:
                    request = (
                        b"POST /v1/traces HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                        b"Content-Type: application/json\r\nTransfer-Encoding: chunked\r\n\r\n"
                        + f"{len(payload):X}\r\n".encode("ascii") + payload + b"\r\n0\r\n\r\n"
                    )
                    with socket.create_connection(("127.0.0.1", receiver.port), timeout=2) as client:
                        client.sendall(request)
                        self.assertIn(b"200", client.recv(1024))
                finally:
                    receiver.stop()
            self.assertEqual(source_otel.index("copilot", database)[0]["id"], "chunked-trace")

    def test_source_routing_and_otel_settings_are_separate(self) -> None:
        with patch("session_token_viewer.source_routing_config", return_value=source_routing.default_routing(PROVIDERS)):
            markup = render_settings_page("source-routing")

        self.assertIn("Source Routing", markup)
        for provider in PROVIDERS:
            self.assertIn(f"route_{provider}", markup)
        with patch("session_token_viewer.source_otel_config", return_value=source_otel.default_source_otel()):
            otel_markup = render_settings_page("source-otel")
        self.assertIn("Source OTEL", otel_markup)
        self.assertIn("OTLP/HTTP (protobuf or JSON)", otel_markup)

    def test_legacy_database_is_not_used_for_database_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy_path = root / "AI-Tool-Session-Tracker.db"
            legacy_path.write_bytes(b"SQLite format 3\x00")
            with patch("src.common.tracker_database.Path.cwd", return_value=root):
                config_path = config_database_path()
                content_path = content_database_path()

            self.assertTrue(legacy_path.exists())
            self.assertFalse(config_path.exists())
            self.assertFalse(content_path.exists())


if __name__ == "__main__":
    unittest.main()