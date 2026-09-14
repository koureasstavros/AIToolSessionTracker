"""Google Antigravity-specific OpenTelemetry routing and session metadata."""
from __future__ import annotations

PROVIDER = "antigravity"


def matches(attributes: dict[str, object]) -> bool:
    return str(attributes.get("ai.session.provider") or attributes.get("gen_ai.system") or attributes.get("gen_ai.provider.name") or "").lower() in {"google", "gemini", "antigravity"}


def session_id(attributes: dict[str, object], trace_id: str) -> str:
    return str(attributes.get("ai.session.id") or attributes.get("gen_ai.conversation.id") or attributes.get("session.id") or trace_id)


def session_metadata(attribute_sets: list[dict[str, object]], fallback_id: str) -> tuple[str, str | None]:
    project = next((str(attributes[key]) for attributes in attribute_sets for key in ("code.workspace.folder", "workspace.path", "git.repository") if isinstance(attributes.get(key), str) and attributes[key].strip()), None)
    return fallback_id, project
