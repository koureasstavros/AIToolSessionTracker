"""Database and data migrations for the application."""
from __future__ import annotations

import json
from pathlib import Path

from src.migrations import migration_config, migration_config_001, migration_config_002, migration_content

_BUNDLED_MODEL_COSTS = json.loads(
    (Path(__file__).parent.parent / "common" / "model_costs.json").read_text(encoding="utf-8")
)


def run_config_migrations(connection) -> None:
    """Initialize and upgrade the shared configuration database in order."""
    migration_config.migration_source_routing_schema(connection)
    migration_config.migration_source_otel_config_schema(connection)
    migration_config.migration_model_mapping_schema(connection)
    migration_config_001.migration_001_model_costs(connection, _BUNDLED_MODEL_COSTS)
    migration_config_002.migration_002_cache_threshold(connection)


def run_content_migrations(connection) -> None:
    """Initialize the local OTEL content database."""
    migration_content.migration_source_otel_content_schema(connection)
