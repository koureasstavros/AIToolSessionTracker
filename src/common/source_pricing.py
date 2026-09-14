"""Model pricing lookup and token-cost calculation."""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from src.common.tracker_database import config_database_path, connect_database

_SEED_COSTS = json.loads((Path(__file__).with_name("model_costs.json")).read_text(encoding="utf-8"))
_SEED_MAPPINGS = json.loads((Path(__file__).with_name("model_mapping.json")).read_text(encoding="utf-8"))
LEGACY_MODEL_MAPPING_FILENAME = "model_mappings.json"
_PRICING_COST_CACHE: dict[Path, list[dict[str, object]]] = {}


def _canonical(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def mapping_config_path() -> Path:
    """Return the tracker database that persists model deployment mappings."""
    return config_database_path()


def _legacy_mapping_path() -> Path:
    return Path.cwd() / LEGACY_MODEL_MAPPING_FILENAME


def _initialize_mapping_tables(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE IF NOT EXISTS model_mappings (deployment TEXT PRIMARY KEY COLLATE NOCASE, model TEXT NOT NULL)"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS tracker_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )


def _initialize_cost_tables(connection: sqlite3.Connection) -> None:
    _initialize_mapping_tables(connection)
    connection.execute(
        """CREATE TABLE IF NOT EXISTS model_costs (
            model TEXT PRIMARY KEY COLLATE NOCASE,
            vendor TEXT NOT NULL,
            input REAL NOT NULL,
            cache_read REAL,
            cache_write REAL,
            output REAL NOT NULL,
            reasoning REAL
        )"""
    )


def _seed_model_costs(connection: sqlite3.Connection) -> None:
    """Import bundled model costs once, leaving later database edits untouched."""
    seeded = connection.execute(
        "SELECT 1 FROM tracker_metadata WHERE key = 'model_costs_json_seeded'"
    ).fetchone()
    if seeded:
        return
    connection.executemany(
        """INSERT OR IGNORE INTO model_costs
        (model, vendor, input, cache_read, cache_write, output, reasoning)
        VALUES (:model, :vendor, :input, :cache_read, :cache_write, :output, :reasoning)""",
        _SEED_COSTS,
    )
    connection.execute(
        "INSERT INTO tracker_metadata (key, value) VALUES ('model_costs_json_seeded', '1')"
    )


def load_model_costs(path: Path | None = None) -> list[dict[str, object]]:
    """Load database-backed model prices in USD per million tokens."""
    try:
        with connect_database(path or mapping_config_path()) as connection, connection:
            _initialize_cost_tables(connection)
            _seed_model_costs(connection)
            rows = connection.execute(
                "SELECT vendor, model, input, cache_read, cache_write, output, reasoning FROM model_costs ORDER BY vendor COLLATE NOCASE, model COLLATE NOCASE"
            ).fetchall()
    except (OSError, sqlite3.Error):
        return []
    keys = ("vendor", "model", "input", "cache_read", "cache_write", "output", "reasoning")
    return [dict(zip(keys, row)) for row in rows]


def save_model_costs(costs: list[dict[str, object]], path: Path | None = None) -> Path:
    """Replace the editable model price catalog after validating all entries."""
    cleaned = []
    for cost in costs:
        model = str(cost.get("model") or "").strip()
        vendor = str(cost.get("vendor") or "Custom").strip() or "Custom"
        if not model:
            continue
        row: dict[str, object] = {"vendor": vendor, "model": model}
        for key in ("input", "cache_read", "cache_write", "output", "reasoning"):
            value = cost.get(key)
            if value in (None, ""):
                row[key] = None
                continue
            try:
                numeric = float(str(value))
            except (TypeError, ValueError) as error:
                raise ValueError(f"{model}: {key} must be a number") from error
            if numeric < 0:
                raise ValueError(f"{model}: {key} cannot be negative")
            row[key] = numeric
        if row["input"] is None or row["output"] is None:
            raise ValueError(f"{model}: input and output costs are required")
        cleaned.append(row)
    if len({str(row["model"]).lower() for row in cleaned}) != len(cleaned):
        raise ValueError("Model names must be unique")
    config_path = path or mapping_config_path()
    with connect_database(config_path) as connection, connection:
        _initialize_cost_tables(connection)
        connection.execute("DELETE FROM model_costs")
        connection.executemany(
            """INSERT INTO model_costs
            (vendor, model, input, cache_read, cache_write, output, reasoning)
            VALUES (:vendor, :model, :input, :cache_read, :cache_write, :output, :reasoning)""",
            cleaned,
        )
        connection.execute(
            "INSERT OR REPLACE INTO tracker_metadata (key, value) VALUES ('model_costs_json_seeded', '1')"
        )
    _PRICING_COST_CACHE.pop(config_path, None)
    return config_path


def _pricing_costs() -> list[dict[str, object]]:
    """Cache catalog reads used repeatedly while building session cost totals."""
    config_path = mapping_config_path()
    if config_path not in _PRICING_COST_CACHE:
        _PRICING_COST_CACHE[config_path] = load_model_costs(config_path)
    return _PRICING_COST_CACHE[config_path]


def _migrate_legacy_mappings(connection: sqlite3.Connection) -> None:
    """Copy the former JSON mappings exactly once to avoid losing user settings."""
    migrated = connection.execute(
        "SELECT 1 FROM tracker_metadata WHERE key = 'model_mappings_json_migrated'"
    ).fetchone()
    if migrated:
        return
    try:
        payload = json.loads(_legacy_mapping_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        payload = {}
    mappings = payload.get("mappings") if isinstance(payload, dict) else {}
    if isinstance(mappings, dict):
        connection.executemany(
            "INSERT OR IGNORE INTO model_mappings (deployment, model) VALUES (?, ?)",
            [
                (str(deployment).strip(), str(model).strip())
                for deployment, model in mappings.items()
                if str(deployment).strip() and str(model).strip()
            ],
        )
    connection.execute(
        "INSERT INTO tracker_metadata (key, value) VALUES ('model_mappings_json_migrated', '1')"
    )


def _seed_model_mappings(connection: sqlite3.Connection) -> None:
    """Import bundled deployment mappings once without replacing local edits."""
    seeded = connection.execute(
        "SELECT 1 FROM tracker_metadata WHERE key = 'model_mapping_json_seeded'"
    ).fetchone()
    if seeded:
        return
    mappings = _SEED_MAPPINGS.get("mappings") if isinstance(_SEED_MAPPINGS, dict) else {}
    if isinstance(mappings, dict):
        connection.executemany(
            "INSERT OR IGNORE INTO model_mappings (deployment, model) VALUES (?, ?)",
            [
                (str(deployment).strip(), str(model).strip())
                for deployment, model in mappings.items()
                if str(deployment).strip() and str(model).strip()
            ],
        )
    connection.execute(
        "INSERT INTO tracker_metadata (key, value) VALUES ('model_mapping_json_seeded', '1')"
    )


def load_model_mappings(path: Path | None = None) -> dict[str, str]:
    """Load deployment-to-pricing-model mappings from the tracker database."""
    try:
        with connect_database(path or mapping_config_path()) as connection, connection:
            _initialize_mapping_tables(connection)
            _migrate_legacy_mappings(connection)
            _seed_model_mappings(connection)
            rows = connection.execute(
                "SELECT deployment, model FROM model_mappings ORDER BY deployment COLLATE NOCASE"
            ).fetchall()
    except (OSError, sqlite3.Error):
        return {}
    return {str(deployment): str(model) for deployment, model in rows}


def save_model_mappings(mappings: dict[str, str], path: Path | None = None) -> Path:
    """Persist deployment mappings in the shared local tracker database."""
    config_path = path or mapping_config_path()
    cleaned = {
        str(deployment).strip(): str(model).strip()
        for deployment, model in mappings.items()
        if str(deployment).strip() and str(model).strip()
    }
    with connect_database(config_path) as connection, connection:
        _initialize_mapping_tables(connection)
        connection.execute("DELETE FROM model_mappings")
        connection.executemany(
            "INSERT INTO model_mappings (deployment, model) VALUES (?, ?)",
            sorted(cleaned.items(), key=lambda item: item[0].lower()),
        )
        connection.execute(
            "INSERT OR REPLACE INTO tracker_metadata (key, value) VALUES ('model_mappings_json_migrated', '1')"
        )
        connection.execute(
            "INSERT OR REPLACE INTO tracker_metadata (key, value) VALUES ('model_mapping_json_seeded', '1')"
        )
    return config_path


def mapped_model(model: object) -> str | None:
    """Return an explicitly configured pricing model for a deployment ID."""
    if not isinstance(model, str) or not model.strip():
        return None
    wanted = _canonical(model)
    for deployment, pricing_model in load_model_mappings().items():
        if _canonical(deployment) == wanted:
            return pricing_model
    return None


def find_model(model: object) -> dict[str, Any] | None:
    """Find a price row, tolerating provider prefixes and deployment suffixes."""
    if not isinstance(model, str) or not model.strip():
        return None
    explicit_mapping = mapped_model(model)
    if explicit_mapping:
        model = explicit_mapping
    wanted = _canonical(model)
    costs = _pricing_costs()
    exact = next((row for row in costs if _canonical(row["model"]) == wanted), None)
    if exact:
        return exact
    # Deployment identifiers commonly contain the public model name.
    matches = [row for row in costs if _canonical(row["model"]) in wanted or wanted in _canonical(row["model"])]
    return max(matches, key=lambda row: len(_canonical(row["model"]))) if matches else None


def cost_for_tokens(tokens: dict[str, object], model: object, output_includes_reasoning: bool = True) -> float | None:
    """Return USD cost; reasoning output is charged at its separate rate."""
    if _canonical(model) == "synthetic":
        return 0.0
    if find_model(model) is None:
        return None
    return sum(cost_breakdown(tokens, model, output_includes_reasoning).values())


def cost_breakdown(tokens: dict[str, object], model: object, output_includes_reasoning: bool = True) -> dict[str, float]:
    """Return USD cost by normalized token category."""
    if _canonical(model) == "synthetic":
        return {
            "inputTokens": 0.0,
            "cacheReadTokens": 0.0,
            "cacheWriteTokens": 0.0,
            "outputTokens": 0.0,
            "reasoningTokens": 0.0,
        }
    price = find_model(model)
    if price is None:
        return {}

    def value(key: str) -> int:
        candidate = tokens.get(key)
        return candidate if isinstance(candidate, int) and not isinstance(candidate, bool) else 0

    output = value("outputTokens")
    reasoning = value("reasoningTokens")
    regular_output = max(0, output - reasoning) if output_includes_reasoning else output
    return {
        "inputTokens": value("inputTokens") * price["input"] / 1_000_000,
        "cacheReadTokens": value("cacheReadTokens") * (price["cache_read"] or 0) / 1_000_000,
        "cacheWriteTokens": value("cacheWriteTokens") * (price["cache_write"] or 0) / 1_000_000,
        "outputTokens": regular_output * price["output"] / 1_000_000,
        "reasoningTokens": reasoning * (price["reasoning"] or price["output"]) / 1_000_000,
    }


def apply_costs(session: dict) -> dict:
    """Annotate usage with model-aware costs, including delegated agents."""
    token_keys = ("inputTokens", "cacheReadTokens", "cacheWriteTokens", "outputTokens", "reasoningTokens")
    model = session.get("model")
    pricing_models: set[str] = set()
    subagents = session.get("subagents", []) if isinstance(session.get("subagents"), list) else []
    for subagent in subagents:
        if isinstance(subagent, dict):
            apply_costs(subagent)
            subagent_model = subagent.get("pricingModel")
            if isinstance(subagent_model, str) and subagent_model != "Mixed":
                pricing_models.add(subagent_model)

    def empty_breakdown() -> dict[str, float]:
        return {key: 0.0 for key in token_keys}

    def add_component(
        target_costs: dict[str, float],
        target_tokens: dict[str, int],
        tokens: object,
        component_model: object,
        output_includes_reasoning: bool = True,
    ) -> bool:
        if not isinstance(tokens, dict):
            return False
        component_costs = cost_breakdown(tokens, component_model, output_includes_reasoning)
        for key in token_keys:
            value = tokens.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                target_tokens[key] += value
            target_costs[key] += component_costs.get(key) or 0.0
        return bool(component_costs)

    linked_agents: set[int] = set()
    session_costs = empty_breakdown()
    session_accounted = {key: 0 for key in token_keys}
    session_has_pricing = False
    for turn in session.get("turns", []):
        turn_model = turn.get("model") or model
        turn["model"] = turn_model
        turn_price = find_model(turn_model)
        if turn_price:
            pricing_models.add(turn_price["model"])
        turn_costs = empty_breakdown()
        turn_accounted = {key: 0 for key in token_keys}
        turn_has_pricing = False
        turn_output_includes_reasoning = not bool(turn.get("outputTokensExcludeReasoning"))
        for invocation in turn.get("invocations", []) if isinstance(turn.get("invocations"), list) else []:
            invocation_model = invocation.get("model") or turn_model
            invocation["model"] = invocation_model
            invocation_output_includes_reasoning = not bool(invocation.get("outputTokensExcludeReasoning"))
            invocation["costUsd"] = cost_for_tokens(
                invocation.get("tokens", {}), invocation_model, invocation_output_includes_reasoning
            )
            invocation_price = find_model(invocation_model)
            if invocation_price:
                pricing_models.add(invocation_price["model"])
            turn_has_pricing = add_component(
                turn_costs, turn_accounted, invocation.get("tokens", {}), invocation_model,
                invocation_output_includes_reasoning,
            ) or turn_has_pricing
            for tool in invocation.get("tools", []) if isinstance(invocation.get("tools"), list) else []:
                agent = tool.get("subagent") if isinstance(tool, dict) and isinstance(tool.get("subagent"), dict) else None
                if agent is None or id(agent) in linked_agents:
                    continue
                linked_agents.add(id(agent))
                agent_model = agent.get("pricingModel") or agent.get("agentModel") or agent.get("model")
                turn_has_pricing = add_component(
                    turn_costs, turn_accounted, agent.get("tokens", {}), agent_model,
                    not bool(agent.get("outputTokensExcludeReasoning")),
                ) or turn_has_pricing
        residual = {
            key: max(0, (turn.get("tokens", {}).get(key) or 0) - turn_accounted[key])
            for key in token_keys
        }
        if any(residual.values()):
            turn_has_pricing = add_component(
                turn_costs, turn_accounted, residual, turn_model, turn_output_includes_reasoning
            ) or turn_has_pricing
        turn["costBreakdown"] = turn_costs if turn_has_pricing else {key: None for key in token_keys}
        turn["costUsd"] = sum(turn_costs.values()) if turn_has_pricing else None
        for key in token_keys:
            session_costs[key] += turn_costs[key]
            session_accounted[key] += turn.get("tokens", {}).get(key) or 0
        session_has_pricing = session_has_pricing or turn_has_pricing

    for subagent in subagents:
        if not isinstance(subagent, dict) or id(subagent) in linked_agents:
            continue
        agent_costs = subagent.get("costBreakdown") if isinstance(subagent.get("costBreakdown"), dict) else {}
        for key in token_keys:
            session_costs[key] += agent_costs.get(key) or 0.0
            session_accounted[key] += subagent.get("tokens", {}).get(key) or 0
        session_has_pricing = session_has_pricing or subagent.get("costUsd") is not None

    session_residual = {
        key: max(0, (session.get("tokens", {}).get(key) or 0) - session_accounted[key])
        for key in token_keys
    }
    if any(session_residual.values()):
        session_has_pricing = add_component(
            session_costs, session_accounted, session_residual, model,
            not bool(session.get("outputTokensExcludeReasoning")),
        ) or session_has_pricing
    # Some providers use placeholders such as ``<synthetic>`` at session
    # level while storing the real model on each turn. Promote a single
    # priced turn model so aggregate cards and sessions without turns can be
    # priced consistently as well.
    if find_model(model) is None and len(pricing_models) == 1:
        model = next(iter(pricing_models))
        session["model"] = model
    session["costBreakdown"] = session_costs if session_has_pricing else {key: None for key in token_keys}
    session["costUsd"] = sum(session_costs.values()) if session_has_pricing else None
    if not pricing_models:
        price = find_model(model)
        session["pricingModel"] = price["model"] if price else None
    elif len(pricing_models) == 1:
        session["pricingModel"] = next(iter(pricing_models))
    else:
        session["pricingModel"] = "Mixed"
    return session
