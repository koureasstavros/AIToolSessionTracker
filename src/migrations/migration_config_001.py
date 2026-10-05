"""Migration 001 for upgrading model-cost data."""
from __future__ import annotations

from src.migrations import migration_config

MIGRATION_001 = "model_costs"


def migration_001_model_costs(connection, bundled_costs: list[dict[str, object]]) -> None:
    """Migration 001: upgrade the model-cost schema and data together."""
    migration_config.migration_model_costs_schema(connection)
    existing_columns = {row[1] for row in connection.execute("PRAGMA table_info(model_costs)").fetchall()}
    additions = {
        "context_threshold": "INTEGER DEFAULT NULL", "cache_write_5m": "REAL", "cache_write_1h": "REAL",
        "short_input": "REAL", "short_cache_read": "REAL", "short_cache_write": "REAL",
        "short_cache_write_5m": "REAL", "short_cache_write_1h": "REAL", "short_output": "REAL",
        "short_reasoning": "REAL", "long_input": "REAL", "long_cache_read": "REAL",
        "long_cache_write": "REAL", "long_cache_write_5m": "REAL", "long_cache_write_1h": "REAL",
        "long_output": "REAL", "long_reasoning": "REAL",
    }
    for column, definition in additions.items():
        if column not in existing_columns:
            connection.execute(f"ALTER TABLE model_costs ADD COLUMN {column} {definition}")
    for tier in ("short", "long"):
        connection.execute(f"""UPDATE model_costs SET
            {tier}_input = COALESCE({tier}_input, input),
            {tier}_cache_read = COALESCE({tier}_cache_read, cache_read),
            {tier}_cache_write = COALESCE({tier}_cache_write, cache_write),
            {tier}_cache_write_5m = COALESCE({tier}_cache_write_5m, {tier}_cache_write),
            {tier}_cache_write_1h = COALESCE({tier}_cache_write_1h, {tier}_cache_write),
            {tier}_output = COALESCE({tier}_output, output),
            {tier}_reasoning = COALESCE({tier}_reasoning, reasoning)""")
    connection.execute("UPDATE model_costs SET cache_write_5m = COALESCE(cache_write_5m, cache_write), cache_write_1h = COALESCE(cache_write_1h, cache_write)")
    if connection.execute("SELECT 1 FROM tracker_metadata WHERE key = 'model_costs_migrated'").fetchone():
        return
    for bundled in bundled_costs:
        threshold = bundled.get("context_threshold")
        if threshold is not None:
            connection.execute("UPDATE model_costs SET context_threshold = ? WHERE model = ? COLLATE NOCASE AND context_threshold IS NULL", (threshold, bundled["model"]))
    connection.execute("INSERT OR REPLACE INTO tracker_metadata (key, value) VALUES ('model_costs_migrated', '1')")
    connection.execute("DELETE FROM tracker_metadata WHERE key IN ('model_costs_json_deleted', 'model_costs_json_seeded')")
