"""Provider-independent deployment-to-model mapping helpers."""
from __future__ import annotations

import re
from collections.abc import Mapping


def canonical(value: object) -> str:
    """Normalize a deployment or model identifier for comparison."""
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def mapped_model(model: object, mappings: Mapping[str, str]) -> str | None:
    """Return the explicitly configured model for a deployment identifier."""
    if not isinstance(model, str) or not model.strip():
        return None
    wanted = canonical(model)
    for deployment, pricing_model in mappings.items():
        if canonical(deployment) == wanted:
            return str(pricing_model)
    return None
