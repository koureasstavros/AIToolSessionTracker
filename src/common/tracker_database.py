"""Shared local database location for AI Tool Session Tracker data."""
from __future__ import annotations

import sqlite3
import threading
from contextlib import closing, contextmanager
from pathlib import Path

CONFIG_DATABASE_FILENAME = "AI-Tool-Session-Tracker-config.db"
CONTENT_DATABASE_FILENAME = "AI-Tool-Session-Tracker-content.db"
_DATABASE_LOCK = threading.RLock()
_WAL_CONFIGURED: set[Path] = set()


def config_database_path() -> Path:
    """Return the app-local database that holds user configuration."""
    return Path.cwd() / CONFIG_DATABASE_FILENAME


def content_database_path() -> Path:
    """Return the app-local database that holds received OTEL content."""
    return Path.cwd() / CONTENT_DATABASE_FILENAME


@contextmanager
def connect_database(path: Path | None = None):
    """Open a serialized local SQLite connection suitable for OTEL writes.

    WAL prevents viewer reads from blocking the OTEL receiver, while the shared
    lock serializes the app's own receiver, scanner, and delete operations.
    """
    resolved_path = path or config_database_path()
    with _DATABASE_LOCK, closing(sqlite3.connect(resolved_path, timeout=15)) as connection:
        connection.execute("PRAGMA busy_timeout = 15000")
        if resolved_path not in _WAL_CONFIGURED:
            connection.execute("PRAGMA journal_mode = WAL")
            _WAL_CONFIGURED.add(resolved_path)
        yield connection