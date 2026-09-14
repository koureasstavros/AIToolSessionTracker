"""Persistent source-routing settings for local and OpenTelemetry sessions."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from src.common.tracker_database import config_database_path, connect_database

ROUTING_FILENAME = "source_routing.json"
_BUNDLED_ROUTING = json.loads((Path(__file__).with_name(ROUTING_FILENAME)).read_text(encoding="utf-8"))
LOCAL_SOURCE = "local"
OTEL_SOURCE = "otel"


def routing_config_path() -> Path:
    """Return the tracker database that persists source routing."""
    return config_database_path()


def default_routing(providers: object) -> dict[str, object]:
    """Create safe local-only defaults for all known providers."""
    keys = providers.keys() if isinstance(providers, dict) else providers
    return {"providers": {str(provider): LOCAL_SOURCE for provider in keys}}


def _initialize_tables(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE IF NOT EXISTS source_routing (provider TEXT PRIMARY KEY, source TEXT NOT NULL)"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS tracker_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )


def _routing_rows(payload: object, defaults: dict[str, object]) -> list[tuple[str, str]]:
    """Normalize the bundled routing payload into safe local rows."""
    configured = payload.get("providers") if isinstance(payload, dict) and isinstance(payload.get("providers"), dict) else {}
    return [
        (
            provider,
            configured.get(provider) if configured.get(provider) in {LOCAL_SOURCE, OTEL_SOURCE} else LOCAL_SOURCE,
        )
        for provider in defaults["providers"]
    ]


def _seed_initial_routing(connection: sqlite3.Connection, defaults: dict[str, object]) -> list[tuple[str, str]]:
    """Seed the bundled source routes into a new local database once."""
    seeded = connection.execute(
        "SELECT 1 FROM tracker_metadata WHERE key = 'source_routing_json_seeded'"
    ).fetchone()
    if seeded:
        return []
    rows = _routing_rows(_BUNDLED_ROUTING, defaults)
    connection.executemany("INSERT OR REPLACE INTO source_routing (provider, source) VALUES (?, ?)", rows)
    connection.execute(
        "INSERT INTO tracker_metadata (key, value) VALUES ('source_routing_json_seeded', '1')"
    )
    return rows


def load_source_routing(providers: object, path: Path | None = None) -> dict[str, object]:
    """Load database-backed routing settings, migrating JSON defaults once."""
    result = default_routing(providers)
    config_path = path or routing_config_path()
    try:
        with connect_database(config_path) as connection, connection:
            _initialize_tables(connection)
            rows = connection.execute("SELECT provider, source FROM source_routing").fetchall()
            if not rows:
                rows = _seed_initial_routing(connection, result)
            if not rows:
                rows = _routing_rows({}, result)
                connection.executemany("INSERT OR REPLACE INTO source_routing (provider, source) VALUES (?, ?)", rows)
    except (OSError, sqlite3.Error, ValueError):
        # Preserve safe local-only startup if an existing database is unavailable.
        return result
    known = result["providers"]
    for provider, source in rows:
        if provider in known and source in {LOCAL_SOURCE, OTEL_SOURCE}:
            known[provider] = source
    return result


def save_source_routing(
    providers: object,
    routes: dict[str, object],
    path: Path | None = None,
) -> Path:
    """Validate and persist provider source routes in SQLite."""
    result = default_routing(providers)
    for provider in result["providers"]:
        route = routes.get(provider)
        if route not in {LOCAL_SOURCE, OTEL_SOURCE}:
            raise ValueError(f"Invalid source route for {provider}")
        result["providers"][provider] = route
    config_path = path or routing_config_path()
    with connect_database(config_path) as connection, connection:
        _initialize_tables(connection)
        connection.execute("DELETE FROM source_routing")
        connection.executemany(
            "INSERT INTO source_routing (provider, source) VALUES (?, ?)",
            list(result["providers"].items()),
        )
        connection.execute(
            "INSERT OR REPLACE INTO tracker_metadata (key, value) VALUES ('source_routing_json_seeded', '1')"
        )
    return config_path
