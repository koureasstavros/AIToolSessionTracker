"""Initial database schemas for local configuration data."""
from __future__ import annotations


def migration_source_routing_schema(connection) -> None:
    """Create the source-routing schema."""
    connection.execute(
        "CREATE TABLE IF NOT EXISTS source_routing (provider TEXT PRIMARY KEY, source TEXT NOT NULL)"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS tracker_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )


def migration_source_otel_config_schema(connection) -> None:
    """Create the persisted OTEL listener configuration schema."""
    connection.execute(
        """CREATE TABLE IF NOT EXISTS source_otel (
            id INTEGER PRIMARY KEY CHECK (id = 1), protocol TEXT NOT NULL,
            host TEXT NOT NULL, port INTEGER NOT NULL
        )"""
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS tracker_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )


def migration_model_mapping_schema(connection) -> None:
    """Create the deployment-to-model mapping table."""
    connection.execute(
        "CREATE TABLE IF NOT EXISTS model_mappings (deployment TEXT PRIMARY KEY COLLATE NOCASE, model TEXT NOT NULL)"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS tracker_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )


def migration_model_costs_schema(connection) -> None:
    """Create the initial model-cost table."""
    connection.execute(
        "CREATE TABLE IF NOT EXISTS tracker_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS model_costs (
            vendor TEXT NOT NULL, model TEXT PRIMARY KEY COLLATE NOCASE,
            context_threshold INTEGER DEFAULT NULL, input REAL NOT NULL,
            cache_read REAL, cache_write REAL, cache_write_5m REAL,
            cache_write_1h REAL, output REAL NOT NULL, reasoning REAL,
            short_input REAL, short_cache_read REAL, short_cache_write REAL,
            short_cache_write_5m REAL, short_cache_write_1h REAL,
            short_output REAL, short_reasoning REAL, long_input REAL,
            long_cache_read REAL, long_cache_write REAL,
            long_cache_write_5m REAL, long_cache_write_1h REAL,
            long_output REAL, long_reasoning REAL
        )"""
    )
