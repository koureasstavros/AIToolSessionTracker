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
    bundled_providers = _BUNDLED_ROUTING.get("providers", {}) if isinstance(_BUNDLED_ROUTING, dict) else {}
    provider_options = {}
    provider_defaults = {}
    for provider in keys:
        configured = bundled_providers.get(str(provider)) if isinstance(bundled_providers, dict) else None
        available = configured.get("available") if isinstance(configured, dict) else None
        provider_options[str(provider)] = [
            source for source in (available if isinstance(available, list) else [LOCAL_SOURCE, OTEL_SOURCE])
            if source in {LOCAL_SOURCE, OTEL_SOURCE}
        ] or [LOCAL_SOURCE]
        configured_default = configured.get("default") if isinstance(configured, dict) else LOCAL_SOURCE
        provider_defaults[str(provider)] = configured_default if configured_default in provider_options[str(provider)] else LOCAL_SOURCE
    available_sources = _BUNDLED_ROUTING.get("available_sources") if isinstance(_BUNDLED_ROUTING, dict) else None
    return {
        "providers": provider_defaults,
        "available_sources": available_sources if isinstance(available_sources, dict) else {LOCAL_SOURCE: "Local storage", OTEL_SOURCE: "OTEL service"},
        "provider_options": provider_options,
    }


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
    rows = []
    options = defaults["provider_options"]
    for provider in defaults["providers"]:
        configured_value = configured.get(provider)
        if isinstance(configured_value, dict):
            allowed = configured_value.get("available")
            if isinstance(allowed, list):
                options[provider] = [source for source in allowed if source in {LOCAL_SOURCE, OTEL_SOURCE}] or [LOCAL_SOURCE]
            configured_value = configured_value.get("default")
        elif isinstance(configured_value, str) and configured_value in {LOCAL_SOURCE, OTEL_SOURCE}:
            options[provider] = [LOCAL_SOURCE, OTEL_SOURCE]
        allowed = options.get(provider, [LOCAL_SOURCE])
        rows.append((provider, configured_value if configured_value in allowed else LOCAL_SOURCE))
    return rows


def _seed_initial_routing(connection: sqlite3.Connection, defaults: dict[str, object]) -> list[tuple[str, str]]:
    """Add bundled source routes that are missing without replacing edits."""
    rows = _routing_rows(_BUNDLED_ROUTING, defaults)
    connection.executemany("INSERT OR IGNORE INTO source_routing (provider, source) VALUES (?, ?)", rows)
    connection.execute(
        "INSERT OR REPLACE INTO tracker_metadata (key, value) VALUES ('source_routing_json_seeded', '1')"
    )
    return rows


def load_source_routing(providers: object, path: Path | None = None) -> dict[str, object]:
    """Load database-backed routing settings, migrating JSON defaults once."""
    result = default_routing(providers)
    config_path = path or routing_config_path()
    try:
        with connect_database(config_path) as connection, connection:
            _initialize_tables(connection)
            _seed_initial_routing(connection, result)
            rows = connection.execute("SELECT provider, source FROM source_routing").fetchall()
    except (OSError, sqlite3.Error, ValueError):
        # Preserve safe local-only startup if an existing database is unavailable.
        return result
    known = result["providers"]
    result["provider_options"] = {
        provider: list(options)
        for provider, options in result["provider_options"].items()
    }
    bundled_sources = _BUNDLED_ROUTING.get("available_sources") if isinstance(_BUNDLED_ROUTING, dict) else None
    if isinstance(bundled_sources, dict):
        result["available_sources"] = {
            source: str(label)
            for source, label in bundled_sources.items()
            if source in {LOCAL_SOURCE, OTEL_SOURCE}
        }
    for provider, source in rows:
        if provider in known and source in result["provider_options"].get(provider, [LOCAL_SOURCE]):
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
        if route not in result["provider_options"].get(provider, [LOCAL_SOURCE]):
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
