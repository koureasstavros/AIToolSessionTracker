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
    if _canonical(model) == "synthetic":
        return 0.0
    if find_model(model) is None:
        return None
    return sum(cost_breakdown(tokens, model).values())


def cost_breakdown(tokens: dict[str, object], model: object) -> dict[str, float]:
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
    reasoning = min(value("reasoningTokens"), output)
    return {
        "inputTokens": value("inputTokens") * price["input"] / 1_000_000,
        "cacheReadTokens": value("cacheReadTokens") * (price["cache_read"] or 0) / 1_000_000,
        "cacheWriteTokens": value("cacheWriteTokens") * (price["cache_write"] or 0) / 1_000_000,
        "outputTokens": (output - reasoning) * price["output"] / 1_000_000,
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
    ) -> bool:
        if not isinstance(tokens, dict):
            return False
        component_costs = cost_breakdown(tokens, component_model)
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
        for invocation in turn.get("invocations", []) if isinstance(turn.get("invocations"), list) else []:
            invocation_model = invocation.get("model") or turn_model
            invocation["model"] = invocation_model
            invocation["costUsd"] = cost_for_tokens(invocation.get("tokens", {}), invocation_model)
            invocation_price = find_model(invocation_model)
            if invocation_price:
                pricing_models.add(invocation_price["model"])
            turn_has_pricing = add_component(
                turn_costs, turn_accounted, invocation.get("tokens", {}), invocation_model
            ) or turn_has_pricing
            for tool in invocation.get("tools", []) if isinstance(invocation.get("tools"), list) else []:
                agent = tool.get("subagent") if isinstance(tool, dict) and isinstance(tool.get("subagent"), dict) else None
                if agent is None or id(agent) in linked_agents:
                    continue
                linked_agents.add(id(agent))
                agent_model = agent.get("pricingModel") or agent.get("agentModel") or agent.get("model")
                turn_has_pricing = add_component(
                    turn_costs, turn_accounted, agent.get("tokens", {}), agent_model
                ) or turn_has_pricing
        residual = {
            key: max(0, (turn.get("tokens", {}).get(key) or 0) - turn_accounted[key])
            for key in token_keys
        }
        if any(residual.values()):
            turn_has_pricing = add_component(turn_costs, turn_accounted, residual, turn_model) or turn_has_pricing
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
        session_has_pricing = add_component(session_costs, session_accounted, session_residual, model) or session_has_pricing
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
