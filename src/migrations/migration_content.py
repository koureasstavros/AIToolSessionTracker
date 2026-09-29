"""Migrations for local OpenTelemetry content storage."""
from __future__ import annotations


def migration_source_otel_content_schema(connection) -> None:
    """Create the local OTEL spans, logs, metrics, and indexes schema."""
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
