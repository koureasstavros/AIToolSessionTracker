"""Local OTLP/HTTP protobuf and JSON receiver storage and session reader.

Span/log parsing that is specific to one provider's OTEL shape (Claude Code's
claude_code.* spans and log events, Copilot's chat/execute_tool spans, Codex's
codex.* log events and spawn_agent delegation) lives in that provider's own
``*_otel_provider.py`` module as optional hook functions. This module owns the
shared pipeline: OTLP ingestion/storage, generic GenAI span parsing, and the
turn/invocation assembly that every provider's session shares.
"""
from __future__ import annotations

import json
import sqlite3
import struct
import time
from pathlib import Path
from typing import Any

from src.common.tracker_database import config_database_path, connect_database, content_database_path
from src.providers import anthropic_claude_otel_provider
from src.providers import github_copilot_otel_provider
from src.providers import openai_codex_otel_provider

OTEL_CONFIG_FILENAME = "source_otel.json"
OTEL_HTTP = "otlp_http"
_BUNDLED_OTEL_CONFIG = json.loads(
    (Path(__file__).with_name(OTEL_CONFIG_FILENAME)).read_text(encoding="utf-8")
)
TOKEN_ALIASES = {
    "inputTokens": ("gen_ai.usage.input_tokens", "gen_ai.usage.prompt_tokens", "input_tokens"),
    "cacheReadTokens": ("gen_ai.usage.cache_read.input_tokens", "gen_ai.usage.cache_read_tokens", "cache_read_tokens"),
    "cacheWriteTokens": ("gen_ai.usage.cache_write.input_tokens", "gen_ai.usage.cache_creation.input_tokens", "gen_ai.usage.cache_write_tokens", "cache_creation_tokens", "cache_write_tokens"),
    "outputTokens": ("gen_ai.usage.output_tokens", "gen_ai.usage.completion_tokens", "output_tokens"),
    "reasoningTokens": ("gen_ai.usage.reasoning.output_tokens", "gen_ai.usage.reasoning_tokens", "codex.usage.reasoning_output_tokens", "reasoning_output_tokens", "thinking_tokens", "thinkingTokens", "reasoningTokens"),
}
OTEL_PROVIDER_ADAPTERS = (
    github_copilot_otel_provider,
    openai_codex_otel_provider,
    anthropic_claude_otel_provider,
)


def default_source_otel() -> dict[str, object]:
    """Return the safe, local-only OTLP listener defaults."""
    return {"protocol": OTEL_HTTP, "host": "127.0.0.1", "port": 4318}


def _valid_otel_port(value: object) -> int | None:
    try:
        port = int(str(value))
    except (TypeError, ValueError):
        return None
    return port if 1 <= port <= 65535 else None


def _initialize_source_otel_table(connection: sqlite3.Connection) -> None:
    connection.execute("""CREATE TABLE IF NOT EXISTS source_otel (
        id INTEGER PRIMARY KEY CHECK (id = 1), protocol TEXT NOT NULL,
        host TEXT NOT NULL, port INTEGER NOT NULL)""")
    connection.execute(
        "CREATE TABLE IF NOT EXISTS tracker_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )


def _normalize_source_otel(value: object) -> dict[str, object]:
    """Validate a JSON OTEL configuration, falling back to loopback defaults."""
    defaults = default_source_otel()
    if not isinstance(value, dict):
        return defaults
    host = value.get("host")
    port = _valid_otel_port(value.get("port"))
    return {
        "protocol": OTEL_HTTP,
        "host": host.strip() if isinstance(host, str) and host.strip() else defaults["host"],
        "port": port or defaults["port"],
    }


def load_source_otel(path: Path | None = None) -> dict[str, object]:
    """Load the globally shared OTLP listener, seeding bundled settings once."""
    config_path = path or config_database_path()
    try:
        with connect_database(config_path) as connection, connection:
            _initialize_source_otel_table(connection)
            row = connection.execute(
                "SELECT protocol, host, port FROM source_otel WHERE id = 1"
            ).fetchone()
            if row:
                return _normalize_source_otel({"protocol": row[0], "host": row[1], "port": row[2]})
            seeded = connection.execute(
                "SELECT 1 FROM tracker_metadata WHERE key = 'source_otel_json_seeded'"
            ).fetchone()
            if not seeded:
                config = _normalize_source_otel(_BUNDLED_OTEL_CONFIG)
                connection.execute(
                    "INSERT INTO source_otel (id, protocol, host, port) VALUES (1, :protocol, :host, :port)",
                    config,
                )
                connection.execute(
                    "INSERT INTO tracker_metadata (key, value) VALUES ('source_otel_json_seeded', '1')"
                )
                return config
    except (OSError, sqlite3.Error):
        return default_source_otel()
    return default_source_otel()


def save_source_otel(protocol: object, host: object, port: object, path: Path | None = None) -> Path:
    """Validate and persist the single local OTLP listener configuration."""
    if protocol != OTEL_HTTP:
        raise ValueError("Only OTLP/HTTP is supported")
    if not isinstance(host, str) or not host.strip():
        raise ValueError("OTLP listener host is required")
    validated_port = _valid_otel_port(port)
    if validated_port is None:
        raise ValueError("OTLP listener port must be between 1 and 65535")
    config_path = path or config_database_path()
    with connect_database(config_path) as connection, connection:
        _initialize_source_otel_table(connection)
        connection.execute(
            "INSERT OR REPLACE INTO source_otel (id, protocol, host, port) VALUES (1, ?, ?, ?)",
            (OTEL_HTTP, host.strip(), validated_port),
        )
        connection.execute(
            "INSERT OR REPLACE INTO tracker_metadata (key, value) VALUES ('source_otel_json_seeded', '1')"
        )
    return config_path


class OtelContext:
    """Mutable state and shared utilities passed into provider OTEL hooks.

    Providers receive this instead of importing this module, so the
    dependency only ever runs one way (this module imports providers).
    """

    def __init__(self, token_keys, tool_arguments, token_values, message_list, parts, part_text) -> None:
        self.pending_tools: dict[str, dict] = {}
        self.internal_spans: list[tuple] = []
        self.pending_subagents: dict[str, dict] = {}
        self.extra: dict[str, object] = {}
        self.token_keys = token_keys
        self.tool_arguments = tool_arguments
        self.token_values = token_values
        self.message_list = message_list
        self.parts = parts
        self.part_text = part_text

    def empty_tokens(self) -> dict[str, None]:
        return {key: None for key in self.token_keys}


def _value(item: object) -> object:
    if not isinstance(item, dict):
        return item
    for key in ("stringValue", "intValue", "doubleValue", "boolValue"):
        if key in item:
            return item[key]
    values = item.get("arrayValue", {}).get("values") if isinstance(item.get("arrayValue"), dict) else None
    return [_value(value) for value in values] if isinstance(values, list) else None


def _attributes(items: object) -> dict[str, object]:
    if not isinstance(items, list):
        return {}
    return {
        str(item.get("key")): _value(item.get("value"))
        for item in items if isinstance(item, dict) and isinstance(item.get("key"), str)
    }


def _provider(attributes: dict[str, object]) -> str | None:
    adapter = next((candidate for candidate in OTEL_PROVIDER_ADAPTERS if candidate.matches(attributes)), None)
    return adapter.PROVIDER if adapter is not None else None


def _provider_adapter(provider: str):
    return next((candidate for candidate in OTEL_PROVIDER_ADAPTERS if candidate.PROVIDER == provider), None)


def _token_values(attributes: dict[str, object]) -> dict[str, int | None]:
    values: dict[str, int | None] = {}
    for key, aliases in TOKEN_ALIASES.items():
        raw = next((attributes[name] for name in aliases if attributes.get(name) is not None), None)
        try:
            values[key] = int(raw) if raw is not None else None
        except (TypeError, ValueError):
            values[key] = None
    # Copilot's OTEL input value includes tokens served from input caches. Keep
    # the cache categories visible while reporting the remaining billable input
    # separately, which also prevents cache tokens from being charged twice.
    if values["inputTokens"] is not None:
        cache_tokens = (values["cacheReadTokens"] or 0) + (values["cacheWriteTokens"] or 0)
        values["inputTokens"] = max(0, values["inputTokens"] - cache_tokens)
    if values["outputTokens"] is not None:
        values["outputTokens"] = max(0, values["outputTokens"] - (values["reasoningTokens"] or 0))
    return values


def _initialize(connection: sqlite3.Connection) -> None:
    connection.execute("""CREATE TABLE IF NOT EXISTS otel_spans (
        provider TEXT NOT NULL, session_id TEXT NOT NULL, trace_id TEXT NOT NULL,
        span_id TEXT NOT NULL, name TEXT NOT NULL, start_ns INTEGER NOT NULL,
        end_ns INTEGER NOT NULL, attributes_json TEXT NOT NULL, raw_json TEXT NOT NULL,
        PRIMARY KEY (trace_id, span_id))""")
    connection.execute("""CREATE TABLE IF NOT EXISTS otel_logs (
        id INTEGER PRIMARY KEY, provider TEXT NOT NULL, session_id TEXT NOT NULL,
        trace_id TEXT, span_id TEXT, timestamp_ns INTEGER NOT NULL, severity TEXT,
        body TEXT, attributes_json TEXT NOT NULL, raw_json TEXT NOT NULL)""")
    connection.execute("""CREATE TABLE IF NOT EXISTS otel_metrics (
        id INTEGER PRIMARY KEY, provider TEXT NOT NULL, session_id TEXT NOT NULL,
        name TEXT NOT NULL, timestamp_ns INTEGER NOT NULL, attributes_json TEXT NOT NULL,
        raw_json TEXT NOT NULL)""")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_otel_logs_session ON otel_logs(provider, session_id)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_otel_metrics_session ON otel_metrics(provider, session_id)")


def _protobuf_fields(payload: bytes) -> list[tuple[int, int, object]]:
    """Read protobuf wire fields needed by the OTLP trace service schema."""
    fields: list[tuple[int, int, object]] = []
    position = 0

    def varint() -> int:
        nonlocal position
        value = 0
        shift = 0
        while position < len(payload) and shift < 70:
            byte = payload[position]
            position += 1
            value |= (byte & 0x7F) << shift
            if not byte & 0x80:
                return value
            shift += 7
        raise ValueError("Invalid OTLP protobuf varint")

    while position < len(payload):
        tag = varint()
        number, wire_type = tag >> 3, tag & 0x07
        if number <= 0:
            raise ValueError("Invalid OTLP protobuf field number")
        if wire_type == 0:
            value: object = varint()
        elif wire_type == 1:
            if position + 8 > len(payload):
                raise ValueError("Truncated OTLP protobuf fixed64 field")
            value = int.from_bytes(payload[position:position + 8], "little")
            position += 8
        elif wire_type == 2:
            length = varint()
            if position + length > len(payload):
                raise ValueError("Truncated OTLP protobuf length-delimited field")
            value = payload[position:position + length]
            position += length
        elif wire_type == 5:
            if position + 4 > len(payload):
                raise ValueError("Truncated OTLP protobuf fixed32 field")
            value = int.from_bytes(payload[position:position + 4], "little")
            position += 4
        else:
            raise ValueError(f"Unsupported OTLP protobuf wire type {wire_type}")
        fields.append((number, wire_type, value))
    return fields


def _protobuf_values(payload: bytes, number: int) -> list[object]:
    return [value for field, _wire_type, value in _protobuf_fields(payload) if field == number]


def _protobuf_text(payload: object) -> str:
    if not isinstance(payload, bytes):
        return ""
    return payload.decode("utf-8", errors="replace")


def _protobuf_any_value(payload: bytes) -> object:
    """Decode the subset of OTLP AnyValue used by Copilot Chat attributes."""
    fields = _protobuf_fields(payload)
    for number, wire_type, value in fields:
        if number == 1 and wire_type == 2:
            return _protobuf_text(value)
        if number == 2 and wire_type == 0:
            return bool(value)
        if number == 3 and wire_type == 0:
            return int(value)
        if number == 4 and wire_type == 1:
            return struct.unpack("<d", int(value).to_bytes(8, "little"))[0]
        if number == 5 and wire_type == 2:
            return [_protobuf_any_value(item) for item in _protobuf_values(value, 1) if isinstance(item, bytes)]
        if number == 7 and wire_type == 2:
            return value.hex() if isinstance(value, bytes) else ""
    return None


def _protobuf_attributes(messages: list[object]) -> list[dict[str, object]]:
    attributes = []
    for message in messages:
        if not isinstance(message, bytes):
            continue
        key_values = _protobuf_values(message, 1)
        value_messages = _protobuf_values(message, 2)
        if not key_values or not value_messages or not isinstance(value_messages[0], bytes):
            continue
        attributes.append({"key": _protobuf_text(key_values[0]), "value": {"stringValue": _protobuf_any_value(value_messages[0])}})
    return attributes


def otlp_protobuf_to_json(payload: bytes) -> dict[str, object]:
    """Convert an OTLP ExportTraceServiceRequest protobuf into the JSON reader shape."""
    resource_spans = []
    for resource_span in _protobuf_values(payload, 1):
        if not isinstance(resource_span, bytes):
            continue
        resource_messages = _protobuf_values(resource_span, 1)
        resource = resource_messages[0] if resource_messages and isinstance(resource_messages[0], bytes) else b""
        scopes = []
        for scope_span in _protobuf_values(resource_span, 2):
            if not isinstance(scope_span, bytes):
                continue
            spans = []
            for span in _protobuf_values(scope_span, 2):
                if not isinstance(span, bytes):
                    continue
                trace_ids = _protobuf_values(span, 1)
                span_ids = _protobuf_values(span, 2)
                names = _protobuf_values(span, 5)
                start_times = _protobuf_values(span, 7)
                end_times = _protobuf_values(span, 8)
                if not trace_ids or not span_ids:
                    continue
                spans.append({
                    "traceId": trace_ids[0].hex() if isinstance(trace_ids[0], bytes) else "",
                    "spanId": span_ids[0].hex() if isinstance(span_ids[0], bytes) else "",
                    "name": _protobuf_text(names[0]) if names else "AI operation",
                    "startTimeUnixNano": str(start_times[0] if start_times else 0),
                    "endTimeUnixNano": str(end_times[0] if end_times else 0),
                    "attributes": _protobuf_attributes(_protobuf_values(span, 9)),
                })
            scopes.append({"spans": spans})
        resource_spans.append({
            "resource": {"attributes": _protobuf_attributes(_protobuf_values(resource, 1))},
            "scopeSpans": scopes,
        })
    return {"resourceSpans": resource_spans}


def _session_metadata(provider: str, attribute_sets: list[dict[str, object]], fallback_id: str) -> tuple[str, str | None]:
    """Resolve provider-specific session metadata through its OTEL mapper."""
    adapter = _provider_adapter(provider)
    if adapter is not None:
        return adapter.session_metadata(attribute_sets, fallback_id)
    return fallback_id, None


def _has_conversation_content(provider: str, attribute_sets: list[dict[str, object]]) -> bool:
    """Exclude infrastructure-only OTEL spans from the session sidebar."""
    for attributes in attribute_sets:
        adapter = _provider_adapter(provider)
        if adapter is not None and getattr(adapter, "is_internal_prompt", lambda _attrs: False)(attributes):
            continue
        if attributes.get("event.name") in {"codex.user_prompt", "claude_code.user_prompt", "user_prompt"} and str(attributes.get("prompt") or "").strip():
            return True
        if any(isinstance(attributes.get(key), str) and attributes[key].strip() for key in (
            "copilot_chat.user_request", "gen_ai.prompt", "ai.session.prompt",
            "gen_ai.completion", "ai.session.response",
        )):
            return True
        for message in _message_list(attributes, "gen_ai.input.messages") + _message_list(attributes, "gen_ai.output.messages"):
            if str(message.get("role") or "").lower() in {"user", "assistant"} and _parts(message):
                return True
    return False


def ingest_otlp_json(payload: object, known_providers: object, path: Path | None = None) -> int:
    """Persist supported OpenTelemetry spans and return the inserted span count."""
    known = set(known_providers.keys() if isinstance(known_providers, dict) else known_providers)
    if not isinstance(payload, dict):
        raise ValueError("OTLP request must contain a JSON object")
    rows: list[tuple[Any, ...]] = []
    for resource_span in payload.get("resourceSpans", []):
        if not isinstance(resource_span, dict):
            continue
        resource_attributes = _attributes(resource_span.get("resource", {}).get("attributes"))
        for scope_span in resource_span.get("scopeSpans", resource_span.get("instrumentationLibrarySpans", [])):
            if not isinstance(scope_span, dict):
                continue
            for span in scope_span.get("spans", []):
                if not isinstance(span, dict):
                    continue
                attributes = {**resource_attributes, **_attributes(span.get("attributes"))}
                provider = _provider(attributes)
                if provider not in known:
                    continue
                trace_id = str(span.get("traceId") or "")
                span_id = str(span.get("spanId") or "")
                if not trace_id or not span_id:
                    continue
                adapter = _provider_adapter(provider)
                session_id = adapter.session_id(attributes, trace_id) if adapter is not None else trace_id
                rows.append((provider, session_id, trace_id, span_id, str(span.get("name") or "AI operation"), int(span.get("startTimeUnixNano") or 0), int(span.get("endTimeUnixNano") or 0), json.dumps(attributes), json.dumps(span)))
    if not rows:
        return 0
    with connect_database(path or content_database_path()) as connection, connection:
        _initialize(connection)
        connection.executemany("INSERT OR REPLACE INTO otel_spans VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    return len(rows)


def _signal_rows(payload: object, resource_key: str, scope_key: str, item_key: str):
    """Yield resource attributes and OTLP signal records from a JSON request."""
    if not isinstance(payload, dict):
        raise ValueError("OTLP request must contain a JSON object")
    for resource_item in payload.get(resource_key, []):
        if not isinstance(resource_item, dict):
            continue
        resource_attributes = _attributes(resource_item.get("resource", {}).get("attributes"))
        for scope_item in resource_item.get(scope_key, resource_item.get("instrumentationLibrary" + scope_key[0].upper() + scope_key[1:], [])):
            if not isinstance(scope_item, dict):
                continue
            for item in scope_item.get(item_key, []):
                if isinstance(item, dict):
                    yield resource_attributes, item


def _body_text(value: object) -> str | None:
    value = _value(value)
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)


def ingest_otlp_logs_json(payload: object, known_providers: object, path: Path | None = None) -> int:
    """Persist OTLP/HTTP JSON log records, including optional raw prompts."""
    known = set(known_providers.keys() if isinstance(known_providers, dict) else known_providers)
    rows: list[tuple[Any, ...]] = []
    for resource_attributes, record in _signal_rows(payload, "resourceLogs", "scopeLogs", "logRecords"):
        attributes = {**resource_attributes, **_attributes(record.get("attributes"))}
        provider = _provider(attributes)
        if provider not in known:
            continue
        trace_id = str(record.get("traceId") or "")
        adapter = _provider_adapter(provider)
        session_id = adapter.session_id(attributes, trace_id) if adapter is not None else trace_id
        rows.append((provider, session_id, trace_id or None, str(record.get("spanId") or "") or None,
                     int(record.get("timeUnixNano") or record.get("observedTimeUnixNano") or 0),
                     str(record.get("severityText") or record.get("severityNumber") or "") or None,
                     _body_text(record.get("body")), json.dumps(attributes), json.dumps(record)))
    if not rows:
        return 0
    with connect_database(path or content_database_path()) as connection, connection:
        _initialize(connection)
        connection.executemany(
            "INSERT INTO otel_logs (provider, session_id, trace_id, span_id, timestamp_ns, severity, body, attributes_json, raw_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
    return len(rows)


def ingest_otlp_metrics_json(payload: object, known_providers: object, path: Path | None = None) -> int:
    """Persist OTLP/HTTP JSON metric points for future session statistics."""
    known = set(known_providers.keys() if isinstance(known_providers, dict) else known_providers)
    rows: list[tuple[Any, ...]] = []
    for resource_attributes, metric in _signal_rows(payload, "resourceMetrics", "scopeMetrics", "metrics"):
        metric_name = str(metric.get("name") or "metric")
        data_points = []
        for value in metric.values():
            if isinstance(value, dict) and isinstance(value.get("dataPoints"), list):
                data_points.extend(point for point in value["dataPoints"] if isinstance(point, dict))
        for point in data_points or [{}]:
            attributes = {**resource_attributes, **_attributes(point.get("attributes"))}
            provider = _provider(attributes)
            if provider not in known:
                continue
            adapter = _provider_adapter(provider)
            session_id = adapter.session_id(attributes, "") if adapter is not None else ""
            if not session_id:
                continue
            rows.append((provider, session_id, metric_name, int(point.get("timeUnixNano") or 0),
                         json.dumps(attributes), json.dumps({"metric": metric, "point": point})))
    if not rows:
        return 0
    with connect_database(path or content_database_path()) as connection, connection:
        _initialize(connection)
        connection.executemany(
            "INSERT INTO otel_metrics (provider, session_id, name, timestamp_ns, attributes_json, raw_json) VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )
    return len(rows)


def index(provider: str, path: Path | None = None) -> list[dict]:
    """Return OTEL-backed session summaries in the native viewer shape."""
    try:
        with connect_database(path or content_database_path()) as connection:
            _initialize(connection)
            rows = connection.execute("SELECT session_id, MAX(end_ns), COUNT(*) FROM otel_spans WHERE provider = ? GROUP BY session_id", (provider,)).fetchall()
            attribute_rows = connection.execute(
                "SELECT session_id, attributes_json FROM otel_spans WHERE provider = ? ORDER BY start_ns", (provider,)
            ).fetchall()
            log_rows = connection.execute(
                "SELECT session_id, timestamp_ns, attributes_json FROM otel_logs WHERE provider = ? ORDER BY timestamp_ns", (provider,)
            ).fetchall()
    except sqlite3.Error:
        return []
    metadata: dict[str, list[dict[str, object]]] = {}
    for session_id, attributes_json in attribute_rows:
        try:
            metadata.setdefault(str(session_id), []).append(json.loads(attributes_json))
        except json.JSONDecodeError:
            continue
    row_by_session = {str(session_id): [session_id, end_ns, count] for session_id, end_ns, count in rows}
    for stored_session_id, timestamp_ns, attributes_json in log_rows:
        try:
            attributes = json.loads(attributes_json)
        except json.JSONDecodeError:
            continue
        adapter = _provider_adapter(provider)
        effective_session_id = adapter.session_id(attributes, str(stored_session_id)) if adapter is not None else str(stored_session_id)
        if not effective_session_id:
            continue
        metadata.setdefault(effective_session_id, []).append(attributes)
        current = row_by_session.get(effective_session_id)
        if current is None:
            row_by_session[effective_session_id] = [effective_session_id, timestamp_ns, 1]
        else:
            current[1] = max(current[1], timestamp_ns)
            current[2] += 1
    rows = list(row_by_session.values())
    summaries = []
    for session_id, end_ns, count in rows:
        attribute_sets = metadata.get(str(session_id), [])
        if not _has_conversation_content(provider, attribute_sets):
            continue
        name, project = _session_metadata(provider, attribute_sets, str(session_id))
        summaries.append({"id": session_id, "name": name, "project": project, "updated": end_ns / 1_000_000_000, "_kind": "otel", "_route": "otel", "_source_label": "OTEL service", "source": "OTEL service", "_has_data": count > 0})
    return summaries


def delete(summary: dict, provider: str, path: Path | None = None) -> bool:
    """Delete every locally stored OTEL span belonging to one provider session."""
    session_id = str(summary.get("id") or "")
    if not session_id:
        return False
    with connect_database(path or content_database_path()) as connection, connection:
        _initialize(connection)
        deleted = connection.execute(
            "DELETE FROM otel_spans WHERE provider = ? AND session_id = ?", (provider, session_id)
        ).rowcount
    return bool(deleted)


def _message_list(attributes: dict[str, object], key: str) -> list[dict]:
    """Read the JSON GenAI message arrays optionally captured by Copilot Chat."""
    value = attributes.get(key)
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return []
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _parts(message: dict) -> list[dict]:
    value = message.get("parts")
    return [part for part in value if isinstance(part, dict)] if isinstance(value, list) else []


def _part_text(part: dict) -> str:
    value = part.get("content") or part.get("text") or part.get("response")
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value) if value is not None else ""


def _tool_arguments(value: object) -> object:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def details(summary: dict, provider: str, path: Path | None = None) -> dict:
    """Convert stored spans into a viewer-compatible session and invocations."""
    session_id = str(summary.get("id") or "")
    adapter = _provider_adapter(provider)
    try:
        with connect_database(path or content_database_path()) as connection:
            _initialize(connection)
            log_rows = connection.execute("SELECT session_id, trace_id, timestamp_ns, attributes_json, raw_json FROM otel_logs WHERE provider = ?", (provider,)).fetchall()
            related_trace_ids = set()
            for stored_session_id, trace_id, _timestamp_ns, attributes_json, _raw_json in log_rows:
                try:
                    attributes = json.loads(attributes_json)
                except json.JSONDecodeError:
                    continue
                effective_session_id = adapter.session_id(attributes, str(stored_session_id)) if adapter is not None else str(stored_session_id)
                if effective_session_id == session_id and trace_id:
                    related_trace_ids.add(str(trace_id))
            query = "SELECT name, start_ns, end_ns, attributes_json, raw_json FROM otel_spans WHERE provider = ? AND (session_id = ?"
            query_values: list[object] = [provider, session_id]
            if related_trace_ids:
                query += " OR trace_id IN (" + ",".join("?" for _ in related_trace_ids) + ")"
                query_values.extend(sorted(related_trace_ids))
            query += ") ORDER BY start_ns"
            rows = connection.execute(query, query_values).fetchall()
            filter_span_rows = getattr(adapter, "filter_span_rows", None) if adapter is not None else None
            if filter_span_rows is not None:
                rows = filter_span_rows(rows, related_trace_ids)
    except sqlite3.Error:
        rows = []
        log_rows = []

    ctx = OtelContext(list(TOKEN_ALIASES), _tool_arguments, _token_values, _message_list, _parts, _part_text)
    tokens = {key: 0 for key in TOKEN_ALIASES}
    token_fields: set[str] = set()
    turns = []
    model = None
    attribute_sets = []
    handle_span = getattr(adapter, "handle_span", None) if adapter is not None else None
    split_user_request = getattr(adapter, "split_user_request", None) if adapter is not None else None
    delegated_agent_from_tool = getattr(adapter, "delegated_agent_from_tool", None) if adapter is not None else None

    for index_value, (name, start_ns, end_ns, attributes_json, raw_json) in enumerate(rows, 1):
        attributes = json.loads(attributes_json)
        if handle_span is not None and handle_span(ctx, name, start_ns, end_ns, attributes, raw_json):
            continue
        attribute_sets.append(attributes)
        model = model or attributes.get("gen_ai.request.model") or attributes.get("gen_ai.response.model")
        span_tokens = _token_values(attributes)
        for key, value in span_tokens.items():
            if value is not None:
                tokens[key] += value
                token_fields.add(key)
        input_messages = _message_list(attributes, "gen_ai.input.messages")
        output_messages = _message_list(attributes, "gen_ai.output.messages")
        user_messages = []
        turn_context: list[dict[str, str]] = []
        assistant_messages = []
        invocation_tools: list[dict] = []
        for message in input_messages:
            role = str(message.get("role") or "").lower()
            for part in _parts(message):
                if role == "user":
                    text = _part_text(part)
                    if text:
                        if split_user_request is not None:
                            request, context = split_user_request(text)
                        else:
                            request, context = text, []
                        if request:
                            user_messages.append(request)
                        turn_context.extend(context)
                elif role == "tool" and part.get("type") == "tool_call_response":
                    tool_id = str(part.get("id") or "")
                    tool = ctx.pending_tools.get(tool_id)
                    if tool is None:
                        tool = {"id": tool_id, "name": "tool", "status": "completed"}
                        invocation_tools.append(tool)
                    tool["result"] = _part_text(part)
                    tool["status"] = "completed"
        for message in output_messages:
            for part in _parts(message):
                part_type = str(part.get("type") or "")
                if part_type == "tool_call":
                    tool = {
                        "id": str(part.get("id") or ""), "name": str(part.get("name") or "tool"),
                        "arguments": _tool_arguments(part.get("arguments")), "status": "requested",
                    }
                    agent = delegated_agent_from_tool(ctx, tool) if delegated_agent_from_tool is not None else None
                    if agent is not None:
                        tool["subagent"] = agent
                    invocation_tools.append(tool)
                    if tool["id"]:
                        ctx.pending_tools[tool["id"]] = tool
                elif str(message.get("role") or "").lower() == "assistant":
                    text = _part_text(part)
                    if text:
                        assistant_messages.append(text)
        if not user_messages:
            fallback = attributes.get("gen_ai.prompt") or attributes.get("ai.session.prompt")
            if fallback:
                user_messages.append(str(fallback))
        if not assistant_messages:
            fallback = attributes.get("gen_ai.completion") or attributes.get("ai.session.response")
            if fallback:
                assistant_messages.append(str(fallback))
        span_fields = [key for key, value in span_tokens.items() if value is not None]
        invocation = {
            "index": 1, "model": model, "assistant": assistant_messages, "tokens": span_tokens,
            "tokenFields": span_fields, "tools": invocation_tools, "outputTokensExcludeReasoning": True,
        }
        turns.append({
            "id": str(index_value), "turn_index": index_value, "timestamp": start_ns / 1_000_000_000,
            "user": "\n\n".join(user_messages), "assistant": assistant_messages, "tools": [],
            "raw": [raw_json], "tokens": span_tokens, "tokenFields": span_fields,
            "invocations": [invocation], "_otel_start": start_ns, "_otel_end": end_ns,
            "_otel_name": name, "internalInstructions": turn_context, "outputTokensExcludeReasoning": True,
        })

    model = model or ctx.extra.get("model")

    precompute_logs = getattr(adapter, "precompute_logs", None) if adapter is not None else None
    if precompute_logs is not None:
        precompute_logs(ctx, log_rows)
    is_user_prompt_event = getattr(adapter, "is_user_prompt_event", None) if adapter is not None else None
    handle_log_event = getattr(adapter, "handle_log_event", None) if adapter is not None else None

    log_turn: dict | None = None
    for stored_session_id, _trace_id, timestamp_ns, attributes_json, raw_json in log_rows:
        try:
            attributes = json.loads(attributes_json)
        except json.JSONDecodeError:
            continue
        effective_session_id = adapter.session_id(attributes, str(stored_session_id)) if adapter is not None else str(stored_session_id)
        if effective_session_id != session_id:
            continue
        event_name = str(attributes.get("event.name") or "")
        prompt = str(attributes.get("prompt") or "").strip()
        is_prompt_event = is_user_prompt_event(event_name) if is_user_prompt_event is not None else event_name == "user_prompt"
        if is_prompt_event and prompt:
            log_turn = {
                "id": f"log-{len(turns) + 1}", "turn_index": len(turns) + 1,
                "timestamp": timestamp_ns / 1_000_000_000, "user": prompt, "assistant": [], "tools": [],
                "raw": [raw_json], "tokens": ctx.empty_tokens(), "tokenFields": [],
                "invocations": [{"index": 1, "model": attributes.get("model"), "assistant": [], "tokens": ctx.empty_tokens(), "tokenFields": [], "tools": []}],
                "internalInstructions": [], "outputTokensExcludeReasoning": True,
            }
            turns.append(log_turn)
            continue
        if log_turn is None:
            continue
        invocation = log_turn["invocations"][0]
        log_turn["raw"].append(raw_json)
        if handle_log_event is not None:
            handle_log_event(ctx, event_name, attributes, raw_json, log_turn, invocation)
    # Copilot progress generation is an internal model call. If the same
    # session contains real user activity, retain it as an invocation on the
    # nearest real turn without presenting its generated status text as user
    # input. Pure helper-only sessions remain hidden by the sidebar filter.
    if ctx.internal_spans and turns:
        for name, start_ns, end_ns, attributes, raw_json in ctx.internal_spans:
            output_messages = _message_list(attributes, "gen_ai.output.messages")
            assistant = [
                _part_text(part) for message in output_messages for part in _parts(message)
                if str(message.get("role") or "").lower() == "assistant" and _part_text(part)
            ]
            invocation_tokens = _token_values(attributes)
            target = max(
                (turn for turn in turns if turn.get("_otel_start", 0) <= start_ns),
                key=lambda turn: turn.get("_otel_start", 0),
                default=turns[0],
            )
            invocation = {
                "index": len(target.get("invocations", [])) + 1,
                "model": attributes.get("gen_ai.request.model") or attributes.get("gen_ai.response.model"),
                "assistant": [], "internalOutput": assistant, "tokens": invocation_tokens,
                "tokenFields": [key for key, value in invocation_tokens.items() if value is not None],
                "tools": [], "kind": "internal", "outputTokensExcludeReasoning": True,
            }
            target.setdefault("invocations", []).append(invocation)
            target.setdefault("raw", []).append(raw_json)
    # An agent span wraps each user interaction and nested model/tool spans
    # for every round (Copilot's invoke_agent/chat), while other providers'
    # log-derived turn and its token-bearing spans arrive as separate,
    # unnested rows (Codex's user_prompt log plus its handle_responses
    # spans). Both need the same fix: grouping by time window keeps
    # tool-only rounds with their original prompt while still exposing
    # their individual models, tokens, tools, and raw events, and merges a
    # session's later, otherwise-turnless spans back into it.
    parent_turns = [
        turn for turn in turns
        if "invoke_agent" in str(turn.get("_otel_name") or "").lower() and turn.get("user")
    ]
    assigned_children: set[str] = set()
    grouped_turns = []
    for parent in parent_turns:
        children = [
            turn for turn in turns
            if turn is not parent
            and str(turn.get("id")) not in assigned_children
            and parent.get("_otel_start", 0) <= turn.get("_otel_start", 0)
            and turn.get("_otel_end", 0) <= parent.get("_otel_end", 0)
        ]
        if not children:
            grouped_turns.append(parent)
            continue
        assigned_children.update(str(turn.get("id")) for turn in children)
        invocations = [
            invocation for child in children for invocation in child.get("invocations", [])
            if isinstance(invocation, dict)
        ]
        for invocation_index, invocation in enumerate(invocations, 1):
            invocation["index"] = invocation_index
        parent["invocations"] = invocations
        parent["raw"] = [raw for turn in [parent, *children] for raw in turn.get("raw", [])]
        if not parent.get("user"):
            parent["user"] = next((turn.get("user") for turn in children if turn.get("user")), "")
        parent["internalInstructions"] = [
            instruction for turn in [parent, *children]
            for instruction in turn.get("internalInstructions", [])
            if isinstance(instruction, dict)
        ]
        if not parent.get("assistant"):
            parent["assistant"] = [
                message for turn in children for message in turn.get("assistant", []) if message
            ]
        parent["tokens"] = {
            key: sum((invocation.get("tokens", {}).get(key) or 0) for invocation in invocations)
            for key in TOKEN_ALIASES
        }
        parent["tokenFields"] = [
            key for key in TOKEN_ALIASES if any(key in invocation.get("tokenFields", []) for invocation in invocations)
        ]
        grouped_turns.append(parent)
    fallback_turn: dict | None = None
    orphaned_turns = sorted(
        [
            turn for turn in turns
            if turn not in parent_turns and str(turn.get("id")) not in assigned_children
        ],
        key=lambda turn: turn.get("_otel_start", 0),
    )
    for turn in orphaned_turns:
        # A user-facing turn's own token-bearing spans can be emitted after
        # the turn's log/span already closed (Copilot's chat spans after
        # their parent agent span ends; Codex's handle_responses spans,
        # which never carry a turn of their own). Start a fallback turn at
        # the next user message and attach its following tool/model rounds
        # until another user message arrives.
        if fallback_turn is None or turn.get("user"):
            fallback_turn = turn
            grouped_turns.append(fallback_turn)
            continue
        fallback_turn["invocations"].extend(turn.get("invocations", []))
        fallback_turn["raw"].extend(turn.get("raw", []))
        fallback_turn["assistant"].extend(turn.get("assistant", []))
        fallback_turn["internalInstructions"].extend(turn.get("internalInstructions", []))
        fallback_turn["tokens"] = {
            key: (fallback_turn.get("tokens", {}).get(key) or 0) + (turn.get("tokens", {}).get(key) or 0)
            for key in TOKEN_ALIASES
        }
        fallback_turn["tokenFields"] = sorted(
            set(fallback_turn.get("tokenFields", [])) | set(turn.get("tokenFields", []))
        )
    for turn in grouped_turns:
        for invocation_index, invocation in enumerate(turn.get("invocations", []), 1):
            if isinstance(invocation, dict):
                invocation["index"] = invocation_index
    turns = sorted(grouped_turns, key=lambda turn: turn.get("_otel_start", 0))
    for index_value, turn in enumerate(turns, 1):
        turn["turn_index"] = index_value
        invocations = turn.get("invocations", [])
        if len(invocations) > 1:
            turn["invocations"] = [
                invocation for invocation in invocations
                if invocation.get("assistant") or invocation.get("tools")
                or any(value is not None for value in invocation.get("tokens", {}).values())
            ]
        if not turn.get("invocations"):
            turn["invocations"] = [{"index": 1, "tokens": {key: None for key in TOKEN_ALIASES}, "assistant": [], "tools": []}]
        for invocation_index, invocation in enumerate(turn["invocations"], 1):
            invocation["index"] = invocation_index
        turn["tools"] = [
            tool for invocation in turn.get("invocations", []) if isinstance(invocation, dict)
            for tool in invocation.get("tools", []) if isinstance(tool, dict)
        ]
    tokens = {
        key: sum((turn.get("tokens", {}).get(key) or 0) for turn in turns)
        for key in TOKEN_ALIASES
    }
    token_fields = {key for turn in turns for key in turn.get("tokenFields", [])}
    name, project = _session_metadata(provider, attribute_sets, session_id)
    return {"id": session_id, "name": summary.get("name") or name, "project": summary.get("project") or project, "updated": max((row[2] for row in rows), default=int(time.time() * 1_000_000_000)) / 1_000_000_000, "turns": turns, "tokens": tokens, "tokenFields": list(token_fields), "model": str(model) if model else None, "provider": provider, "_kind": "otel", "_route": "otel", "_source_label": "OTEL service", "source": "OTEL service", "outputTokensExcludeReasoning": True}
