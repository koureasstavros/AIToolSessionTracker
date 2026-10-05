"""Migration 002: persist model cache-threshold support."""
from __future__ import annotations

MIGRATION_002 = "cache_threshold"


def migration_002_cache_threshold(connection) -> None:
    """Add and initialize the cache-threshold flag for existing model prices."""
    columns = {row[1] for row in connection.execute("PRAGMA table_info(model_costs)").fetchall()}
    if "cache_threshold" not in columns:
        connection.execute("ALTER TABLE model_costs ADD COLUMN cache_threshold INTEGER NOT NULL DEFAULT 0")
    if connection.execute("SELECT 1 FROM tracker_metadata WHERE key = 'cache_threshold_migrated'").fetchone():
        return
    connection.execute(
        """UPDATE model_costs SET cache_threshold = CASE WHEN
            (short_cache_write_1h IS NOT NULL AND short_cache_write_5m IS NOT NULL
                AND short_cache_write_1h != short_cache_write_5m)
            OR (long_cache_write_1h IS NOT NULL AND long_cache_write_5m IS NOT NULL
                AND long_cache_write_1h != long_cache_write_5m)
            OR (cache_write_1h IS NOT NULL AND cache_write_5m IS NOT NULL
                AND cache_write_1h != cache_write_5m)
            THEN 1 ELSE 0 END"""
    )
    connection.execute(
        "INSERT OR REPLACE INTO tracker_metadata (key, value) VALUES ('cache_threshold_migrated', '1')"
    )
