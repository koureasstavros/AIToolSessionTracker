"""Provider-independent deployment-to-model mapping helpers."""
from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterable, Mapping
from pathlib import Path

from src.common.tracker_database import config_database_path, connect_config_database

_SEED_MAPPINGS = json.loads((Path(__file__).with_name("model_mapping.json")).read_text(encoding="utf-8"))
_MODEL_MAPPING_CACHE: dict[Path, dict[str, str]] = {}
_DELETED_MODEL_MAPPINGS: dict[Path, set[str]] = {}


def canonical(value: object) -> str:
    """Normalize a deployment or model identifier for comparison."""
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _seed_model_mappings(connection: sqlite3.Connection, config_path: Path) -> None:
    """Add bundled deployment mappings that are missing without replacing edits."""
    mappings = _SEED_MAPPINGS.get("mappings") if isinstance(_SEED_MAPPINGS, dict) else {}
    deleted = _DELETED_MODEL_MAPPINGS.get(config_path, set())
    if isinstance(mappings, dict):
        connection.executemany(
            "INSERT OR IGNORE INTO model_mappings (deployment, model) VALUES (?, ?)",
            [
                (str(deployment).strip(), str(model).strip())
                for deployment, model in mappings.items()
                if str(deployment).strip()
                and canonical(deployment) not in deleted
                and str(model).strip()
            ],
        )
    connection.execute(
        "INSERT OR REPLACE INTO tracker_metadata (key, value) VALUES ('model_mapping_json_seeded', '1')"
    )


def load_model_mappings(path: Path | None = None) -> dict[str, str]:
    """Load deployment-to-pricing-model mappings from the tracker database."""
    config_path = path or config_database_path()
    cached = _MODEL_MAPPING_CACHE.get(config_path)
    if cached is not None:
        return dict(cached)
    try:
        with connect_config_database(config_path) as connection:
            _seed_model_mappings(connection, config_path)
            rows = connection.execute(
                "SELECT deployment, model FROM model_mappings ORDER BY deployment COLLATE NOCASE"
            ).fetchall()
    except (OSError, sqlite3.Error):
        return {}
    result = {str(deployment): str(model) for deployment, model in rows}
    _MODEL_MAPPING_CACHE[config_path] = result
    return dict(result)


def validate_model_mapping_rows(rows: Iterable[tuple[object, object]]) -> dict[str, str]:
    """Normalize mapping rows without silently discarding duplicate deployments."""
    cleaned: dict[str, str] = {}
    seen: set[str] = set()
    for deployment, model in rows:
        name, model_name = str(deployment).strip(), str(model).strip()
        if not name or not model_name:
            continue
        key = name.lower()
        if key in seen:
            raise ValueError(f"Deployment '{name}' appears more than once. Rename or remove the duplicate before saving.")
        seen.add(key)
        cleaned[name] = model_name
    return cleaned


def save_model_mappings(mappings: dict[str, str], path: Path | None = None) -> Path:
    """Persist deployment mappings in the shared local tracker database."""
    config_path = path or config_database_path()
    cleaned = validate_model_mapping_rows(mappings.items())
    with connect_config_database(config_path) as connection:
        existing_deployments = {
            canonical(row[0])
            for row in connection.execute("SELECT deployment FROM model_mappings").fetchall()
        }
        bundled_deployments = {
            canonical(deployment)
            for deployment in (_SEED_MAPPINGS.get("mappings", {}) if isinstance(_SEED_MAPPINGS, dict) else {})
        }
        submitted_deployments = {canonical(deployment) for deployment in cleaned}
        deleted = set(_DELETED_MODEL_MAPPINGS.get(config_path, set()))
        deleted.difference_update(submitted_deployments)
        deleted.update(existing_deployments.intersection(bundled_deployments) - submitted_deployments)
        _DELETED_MODEL_MAPPINGS[config_path] = deleted
        connection.execute("DELETE FROM model_mappings")
        connection.executemany(
            "INSERT INTO model_mappings (deployment, model) VALUES (?, ?)",
            sorted(cleaned.items(), key=lambda item: item[0].lower()),
        )
        connection.execute(
            "INSERT OR REPLACE INTO tracker_metadata (key, value) VALUES ('model_mapping_json_seeded', '1')"
        )
    _MODEL_MAPPING_CACHE.pop(config_path, None)
    return config_path


def mapped_model(model: object, mappings: Mapping[str, str]) -> str | None:
    """Return the explicitly configured model for a deployment identifier."""
    if not isinstance(model, str) or not model.strip():
        return None
    wanted = canonical(model)
    for deployment, pricing_model in mappings.items():
        if canonical(deployment) == wanted:
            return str(pricing_model)
    return None
