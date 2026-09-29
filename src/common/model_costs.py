"""Model pricing lookup and token-cost calculation."""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from src.common.tracker_database import config_database_path, connect_database
from src.common import model_mapping
from src.migrations import migration_config

_SEED_COSTS = json.loads((Path(__file__).with_name("model_costs.json")).read_text(encoding="utf-8"))
_SEED_MAPPINGS = json.loads((Path(__file__).with_name("model_mapping.json")).read_text(encoding="utf-8"))
_MODEL_COST_CACHE: dict[Path, list[dict[str, object]]] = {}
_MODEL_MAPPING_CACHE: dict[Path, dict[str, str]] = {}
_DELETED_MODEL_COSTS: dict[Path, set[str]] = {}
_DELETED_MODEL_MAPPINGS: dict[Path, set[str]] = {}
CANONICAL_CACHE_WRITE_TOKEN_KEYS = ("cacheWrite5mTokens", "cacheWrite1hTokens")


def _canonical(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def mapping_config_path() -> Path:
    """Return the tracker database that persists model deployment mappings."""
    return config_database_path()


def _seed_model_costs(connection: sqlite3.Connection, config_path: Path) -> None:
    """Add bundled model costs that are missing without overwriting edits."""
    deleted = _DELETED_MODEL_COSTS.get(config_path, set())
    bundled_costs = [
        {
            **row,
            "input": row.get("input", row.get("short_input")),
            "cache_read": row.get("cache_read", row.get("short_cache_read")),
            "cache_write": row.get("cache_write", row.get("short_cache_write")),
            "cache_write_5m": row.get("cache_write_5m", row.get("short_cache_write_5m", row.get("cache_write"))),
            "cache_write_1h": row.get("cache_write_1h", row.get("short_cache_write_1h", row.get("cache_write"))),
            "output": row.get("output", row.get("short_output")),
            "reasoning": row.get("reasoning", row.get("short_reasoning")),
            "context_threshold": row.get("context_threshold"),
            "short_input": row.get("short_input", row.get("input")),
            "short_cache_read": row.get("short_cache_read", row.get("cache_read")),
            "short_cache_write": row.get("short_cache_write", row.get("cache_write")),
            "short_cache_write_5m": row.get("short_cache_write_5m", row.get("cache_write_5m", row.get("cache_write"))),
            "short_cache_write_1h": row.get("short_cache_write_1h", row.get("cache_write_1h", row.get("cache_write"))),
            "short_output": row.get("short_output", row.get("output")),
            "short_reasoning": row.get("short_reasoning", row.get("reasoning")),
            "long_input": row.get("long_input", row.get("input")),
            "long_cache_read": row.get("long_cache_read", row.get("cache_read")),
            "long_cache_write": row.get("long_cache_write", row.get("cache_write")),
            "long_cache_write_5m": row.get("long_cache_write_5m", row.get("cache_write_5m", row.get("cache_write"))),
            "long_cache_write_1h": row.get("long_cache_write_1h", row.get("cache_write_1h", row.get("cache_write"))),
            "long_output": row.get("long_output", row.get("output")),
            "long_reasoning": row.get("long_reasoning", row.get("reasoning")),
        }
        for row in _SEED_COSTS
        if _canonical(row["model"]) not in deleted
    ]
    connection.executemany(
        """INSERT OR IGNORE INTO model_costs
        (model, vendor, input, cache_read, cache_write, cache_write_5m, cache_write_1h, output, reasoning,
         context_threshold,
         short_input, short_cache_read, short_cache_write, short_cache_write_5m, short_cache_write_1h, short_output, short_reasoning,
         long_input, long_cache_read, long_cache_write, long_cache_write_5m, long_cache_write_1h, long_output, long_reasoning)
        VALUES (:model, :vendor, :input, :cache_read, :cache_write, :cache_write_5m, :cache_write_1h, :output, :reasoning,
            :context_threshold,
            :short_input, :short_cache_read, :short_cache_write, :short_cache_write_5m, :short_cache_write_1h, :short_output, :short_reasoning,
            :long_input, :long_cache_read, :long_cache_write, :long_cache_write_5m, :long_cache_write_1h, :long_output, :long_reasoning)""",
        bundled_costs,
    )
    connection.executemany(
        """UPDATE model_costs SET input = :input, cache_read = :cache_read,
            cache_write = :cache_write, cache_write_5m = :cache_write_5m,
            cache_write_1h = :cache_write_1h, output = :output, reasoning = :reasoning,
            short_input = :short_input, short_cache_read = :short_cache_read,
            short_cache_write = :short_cache_write, short_cache_write_5m = :short_cache_write_5m,
            short_cache_write_1h = :short_cache_write_1h, short_output = :short_output,
            short_reasoning = :short_reasoning, long_input = :long_input,
            long_cache_read = :long_cache_read, long_cache_write = :long_cache_write,
            long_cache_write_5m = :long_cache_write_5m, long_cache_write_1h = :long_cache_write_1h,
            long_output = :long_output, long_reasoning = :long_reasoning
            WHERE model = :model AND short_input = input AND long_input = input
            AND short_output = output AND long_output = output""",
        bundled_costs,
    )
    connection.execute(
        "INSERT OR REPLACE INTO tracker_metadata (key, value) "
        "VALUES ('model_costs_seeded', '1')"
    )

def load_model_costs(path: Path | None = None) -> list[dict[str, object]]:
    """Load database-backed model prices in USD per million tokens."""
    try:
        with connect_database(path or mapping_config_path()) as connection, connection:
            migration_config.migration_001_model_costs(connection, _SEED_COSTS)
            _seed_model_costs(connection, path or mapping_config_path())
            rows = connection.execute(
                "SELECT vendor, model, input, cache_read, cache_write, cache_write_5m, cache_write_1h, output, reasoning, context_threshold, short_input, short_cache_read, short_cache_write, short_cache_write_5m, short_cache_write_1h, short_output, short_reasoning, long_input, long_cache_read, long_cache_write, long_cache_write_5m, long_cache_write_1h, long_output, long_reasoning FROM model_costs ORDER BY vendor COLLATE NOCASE, model COLLATE NOCASE"
            ).fetchall()
    except (OSError, sqlite3.Error):
        return []
    keys = ("vendor", "model", "input", "cache_read", "cache_write", "cache_write_5m", "cache_write_1h", "output", "reasoning", "context_threshold", "short_input", "short_cache_read", "short_cache_write", "short_cache_write_5m", "short_cache_write_1h", "short_output", "short_reasoning", "long_input", "long_cache_read", "long_cache_write", "long_cache_write_5m", "long_cache_write_1h", "long_output", "long_reasoning")
    result = [dict(zip(keys, row)) for row in rows]
    for row in result:
        tier_keys = ("input", "cache_read", "cache_write", "output", "reasoning")
        has_tier_difference = any(
            row.get(f"short_{key}") != row.get(f"long_{key}") for key in tier_keys
        )
        if not has_tier_difference:
            row["context_threshold"] = None
    return result


def save_model_costs(costs: list[dict[str, object]], path: Path | None = None) -> Path:
    """Replace the editable model price catalog after validating all entries."""
    cleaned = []
    for cost in costs:
        model = str(cost.get("model") or "").strip()
        vendor = str(cost.get("vendor") or "Custom").strip() or "Custom"
        if not model:
            continue
        row: dict[str, object] = {"vendor": vendor, "model": model}
        for key in ("input", "cache_read", "cache_write", "cache_write_5m", "cache_write_1h", "output", "reasoning", "short_input", "short_cache_read", "short_cache_write", "short_cache_write_5m", "short_cache_write_1h", "short_output", "short_reasoning", "long_input", "long_cache_read", "long_cache_write", "long_cache_write_5m", "long_cache_write_1h", "long_output", "long_reasoning"):
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
        for key in ("context_threshold",):
            value = cost.get(key)
            if value in (None, ""):
                row[key] = None
                continue
            try:
                row[key] = int(str(value))
            except (TypeError, ValueError) as error:
                raise ValueError(f"{model}: {key} must be a whole number") from error
            if row[key] < 0:
                raise ValueError(f"{model}: {key} cannot be negative")
        for tier in ("short", "long"):
            for base_key in ("input", "cache_read", "cache_write", "cache_write_5m", "cache_write_1h", "output", "reasoning"):
                tier_key = f"{tier}_{base_key}"
                if row[tier_key] is None:
                    row[tier_key] = row[base_key]
        row["input"] = row["input"] if row["input"] is not None else row["short_input"]
        row["output"] = row["output"] if row["output"] is not None else row["short_output"]
        row["cache_read"] = row["cache_read"] if row["cache_read"] is not None else row["short_cache_read"]
        row["cache_write"] = row["cache_write"] if row["cache_write"] is not None else row["short_cache_write"]
        row["cache_write_5m"] = row["cache_write_5m"] if row["cache_write_5m"] is not None else row["cache_write"]
        row["cache_write_1h"] = row["cache_write_1h"] if row["cache_write_1h"] is not None else row["cache_write"]
        row["reasoning"] = row["reasoning"] if row["reasoning"] is not None else row["short_reasoning"]
        for tier in ("short", "long"):
            for base_key in ("input", "cache_read", "cache_write", "cache_write_5m", "cache_write_1h", "output", "reasoning"):
                tier_key = f"{tier}_{base_key}"
                if row[tier_key] is None:
                    row[tier_key] = row[base_key]
        if row["input"] is None or row["output"] is None:
            raise ValueError(f"{model}: input and output costs are required")
        cleaned.append(row)
    if len({str(row["model"]).lower() for row in cleaned}) != len(cleaned):
        raise ValueError("Model names must be unique")
    config_path = path or mapping_config_path()
    with connect_database(config_path) as connection, connection:
        migration_config.migration_001_model_costs(connection, _SEED_COSTS)
        existing_models = {
            _canonical(row[0])
            for row in connection.execute("SELECT model FROM model_costs").fetchall()
        }
        bundled_models = {_canonical(row["model"]): str(row["model"]) for row in _SEED_COSTS}
        submitted_models = {_canonical(row["model"]) for row in cleaned}
        deleted_models = existing_models.intersection(bundled_models) - submitted_models
        deleted = set(_DELETED_MODEL_COSTS.get(config_path, set()))
        deleted.difference_update(submitted_models)
        deleted.update(deleted_models)
        _DELETED_MODEL_COSTS[config_path] = deleted
        connection.execute("DELETE FROM model_costs")
        connection.executemany(
            """INSERT INTO model_costs
            (vendor, model, input, cache_read, cache_write, cache_write_5m, cache_write_1h, output, reasoning,
             context_threshold, short_input, short_cache_read,
             short_cache_write, short_cache_write_5m, short_cache_write_1h, short_output, short_reasoning, long_input, long_cache_read,
             long_cache_write, long_cache_write_5m, long_cache_write_1h, long_output, long_reasoning)
            VALUES (:vendor, :model, :input, :cache_read, :cache_write, :cache_write_5m, :cache_write_1h, :output, :reasoning,
                    :context_threshold, :short_input,
                    :short_cache_read, :short_cache_write, :short_cache_write_5m, :short_cache_write_1h, :short_output, :short_reasoning,
                    :long_input, :long_cache_read, :long_cache_write, :long_cache_write_5m, :long_cache_write_1h, :long_output, :long_reasoning)""",
            cleaned,
        )
    _MODEL_COST_CACHE.pop(config_path, None)
    return config_path


def _model_costs() -> list[dict[str, object]]:
    """Cache catalog reads used repeatedly while building session cost totals."""
    config_path = mapping_config_path()
    if config_path not in _MODEL_COST_CACHE:
        _MODEL_COST_CACHE[config_path] = load_model_costs(config_path)
    return _MODEL_COST_CACHE[config_path]


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
                and _canonical(deployment) not in deleted
                and str(model).strip()
            ],
        )
    connection.execute(
        "INSERT OR REPLACE INTO tracker_metadata (key, value) VALUES ('model_mapping_json_seeded', '1')"
    )


def load_model_mappings(path: Path | None = None) -> dict[str, str]:
    """Load deployment-to-pricing-model mappings from the tracker database."""
    config_path = path or mapping_config_path()
    cached = _MODEL_MAPPING_CACHE.get(config_path)
    if cached is not None:
        return dict(cached)
    try:
        with connect_database(config_path) as connection, connection:
            migration_config.migration_model_mapping_schema(connection)
            _seed_model_mappings(connection, config_path)
            rows = connection.execute(
                "SELECT deployment, model FROM model_mappings ORDER BY deployment COLLATE NOCASE"
            ).fetchall()
    except (OSError, sqlite3.Error):
        return {}
    result = {str(deployment): str(model) for deployment, model in rows}
    _MODEL_MAPPING_CACHE[config_path] = result
    return dict(result)


def save_model_mappings(mappings: dict[str, str], path: Path | None = None) -> Path:
    """Persist deployment mappings in the shared local tracker database."""
    config_path = path or mapping_config_path()
    cleaned = {
        str(deployment).strip(): str(model).strip()
        for deployment, model in mappings.items()
        if str(deployment).strip() and str(model).strip()
    }
    with connect_database(config_path) as connection, connection:
        migration_config.migration_model_mapping_schema(connection)
        existing_deployments = {
            _canonical(row[0])
            for row in connection.execute("SELECT deployment FROM model_mappings").fetchall()
        }
        bundled_deployments = {
            _canonical(deployment)
            for deployment in (_SEED_MAPPINGS.get("mappings", {}) if isinstance(_SEED_MAPPINGS, dict) else {})
        }
        submitted_deployments = {_canonical(deployment) for deployment in cleaned}
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


def mapped_model(model: object) -> str | None:
    """Compatibility wrapper for the common deployment mapping helper."""
    return model_mapping.mapped_model(model, load_model_mappings())


def find_model(model: object) -> dict[str, Any] | None:
    """Find a price row, tolerating provider prefixes and deployment suffixes."""
    if not isinstance(model, str) or not model.strip():
        return None
    explicit_mapping = mapped_model(model)
    if explicit_mapping:
        model = explicit_mapping
    wanted = _canonical(model)
    costs = _model_costs()
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


def _context_token_count(tokens: dict[str, object]) -> int:
    """Return the prompt context represented by input and cache token fields."""
    return sum(
        value for key in ("inputTokens", "cacheReadTokens", "cacheWriteTokens")
        for value in [tokens.get(key)]
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0
    )


def _tier_price(price: dict[str, Any], tokens: dict[str, object]) -> dict[str, object]:
    """Select the configured short or long context rate tier."""
    context_tokens = _context_token_count(tokens)
    threshold = price.get("context_threshold")
    tier = "long" if isinstance(threshold, int) and context_tokens >= threshold else "short"
    tier_keys = ("input", "cache_read", "cache_write", "cache_write_5m", "cache_write_1h", "output", "reasoning")
    if any(price.get(f"{tier}_{key}") is not None for key in tier_keys):
        return {key: price.get(f"{tier}_{key}") for key in tier_keys}
    return price


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
    price = _tier_price(price, tokens)

    def value(key: str) -> int:
        candidate = tokens.get(key)
        return candidate if isinstance(candidate, int) and not isinstance(candidate, bool) else 0

    output = value("outputTokens")
    reasoning = value("reasoningTokens")
    regular_output = max(0, output - reasoning) if output_includes_reasoning else output
    cache_read_price = price["cache_read"] if price["cache_read"] is not None else price["input"]
    cache_write_price = price["cache_write"] if price["cache_write"] is not None else price["input"]
    cache_write_5m_price = price.get("cache_write_5m") or cache_write_price
    cache_write_1h_price = price.get("cache_write_1h") or cache_write_price
    cache_write_5m = value("cacheWrite5mTokens")
    cache_write_1h = value("cacheWrite1hTokens")
    cache_write = value("cacheWriteTokens") if not (cache_write_5m or cache_write_1h) else 0
    return {
        "inputTokens": value("inputTokens") * price["input"] / 1_000_000,
        "cacheReadTokens": value("cacheReadTokens") * cache_read_price / 1_000_000,
        "cacheWrite5mTokens": cache_write_5m * cache_write_5m_price / 1_000_000,
        "cacheWrite1hTokens": cache_write_1h * cache_write_1h_price / 1_000_000,
        "cacheWriteTokens": cache_write * cache_write_price / 1_000_000,
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
        # Detailed Anthropic cache-creation costs are returned separately by
        # cost_breakdown(), but the normalized cost breakdown keeps the
        # provider-neutral five-category shape. Roll both TTL-specific costs
        # into the shared cache-write cost bucket.
        target_costs["cacheWriteTokens"] += sum(
            component_costs.get(key) or 0.0
            for key in CANONICAL_CACHE_WRITE_TOKEN_KEYS
        )
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
