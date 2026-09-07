"""Model pricing lookup and token-cost calculation."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

_COSTS = json.loads((Path(__file__).with_name("model_costs.json")).read_text(encoding="utf-8"))


def _canonical(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def find_model(model: object) -> dict[str, Any] | None:
    """Find a price row, tolerating provider prefixes and deployment suffixes."""
    if not isinstance(model, str) or not model.strip():
        return None
    wanted = _canonical(model)
    exact = next((row for row in _COSTS if _canonical(row["model"]) == wanted), None)
    if exact:
        return exact
    # Deployment identifiers commonly contain the public model name.
    matches = [row for row in _COSTS if _canonical(row["model"]) in wanted or wanted in _canonical(row["model"])]
    return max(matches, key=lambda row: len(_canonical(row["model"]))) if matches else None


def cost_for_tokens(tokens: dict[str, object], model: object) -> float | None:
    """Return USD cost; reasoning output is charged at its separate rate."""
    if find_model(model) is None:
        return None
    return sum(cost_breakdown(tokens, model).values())


def cost_breakdown(tokens: dict[str, object], model: object) -> dict[str, float]:
    """Return USD cost by normalized token category."""
    price = find_model(model)
    if price is None:
        return {}

    def value(key: str) -> int:
        candidate = tokens.get(key)
        return candidate if isinstance(candidate, int) and not isinstance(candidate, bool) else 0

    output = value("outputTokens")
    reasoning = min(value("reasoningTokens"), output)
    return {
        "inputTokens": value("inputTokens") * price["input"] / 1_000_000,
        "cacheReadTokens": value("cacheReadTokens") * (price["cache_read"] or 0) / 1_000_000,
        "cacheWriteTokens": value("cacheWriteTokens") * (price["cache_write"] or 0) / 1_000_000,
        "outputTokens": (output - reasoning) * price["output"] / 1_000_000,
        "reasoningTokens": reasoning * (price["reasoning"] or price["output"]) / 1_000_000,
    }


def apply_costs(session: dict) -> dict:
    """Annotate a normalized session, turns, and invocations with USD costs."""
    model = session.get("model")
    pricing_models: set[str] = set()
    for turn in session.get("turns", []):
        turn_model = turn.get("model") or model
        turn["model"] = turn_model
        turn_price = find_model(turn_model)
        if turn_price:
            pricing_models.add(turn_price["model"])
        for invocation in turn.get("invocations", []) if isinstance(turn.get("invocations"), list) else []:
            invocation_model = invocation.get("model") or turn_model
            invocation["model"] = invocation_model
            invocation["costUsd"] = cost_for_tokens(invocation.get("tokens", {}), invocation_model)
        turn["costUsd"] = cost_for_tokens(turn.get("tokens", {}), turn_model)
    if session.get("turns"):
        costs = [turn.get("costUsd") for turn in session["turns"]]
        session["costUsd"] = sum(value for value in costs if isinstance(value, (int, float))) if any(value is not None for value in costs) else None
    else:
        session["costUsd"] = cost_for_tokens(session.get("tokens", {}), model)
    if not pricing_models:
        price = find_model(model)
        session["pricingModel"] = price["model"] if price else None
    elif len(pricing_models) == 1:
        session["pricingModel"] = next(iter(pricing_models))
    else:
        session["pricingModel"] = "Mixed"
    return session
