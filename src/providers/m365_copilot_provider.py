"""Microsoft 365 Copilot transcript discovery and parsing.

Microsoft 365 Copilot does not expose a stable local transcript format. This
adapter intentionally reads JSONL/JSON transcript exports from a configured
local folder and reuses the viewer's generic message and usage parser.
"""
from pathlib import Path

from .source_archive import create_archive, inject_archive


def _viewer():
    import session_token_viewer as viewer
    return viewer


def default_root() -> Path:
    """Return the configured local M365 Copilot export directory."""
    import os
    configured = os.environ.get("M365_COPILOT_ROOT")
    return Path(configured).expanduser() if configured else Path.home() / "m365-copilot" / "sessions"


def display_root(root: Path) -> Path:
    return default_root()


def tool(summary: dict) -> str:
    return "Extension"


def identity(record: dict, fallback: str) -> tuple[str, str]:
    session_id = str(record.get("sessionId") or record.get("session_id") or record.get("conversationId") or record.get("conversation_id") or fallback)
    return session_id, str(record.get("title") or record.get("name") or record.get("summary") or record.get("conversationTitle") or session_id)


def _has_data(path: Path) -> bool:
    for record in _viewer().safe_json_lines(path):
        messages = record.get("messages")
        candidates = messages if isinstance(messages, list) else [record]
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            payload = candidate.get("payload", candidate)
            if not isinstance(payload, dict):
                continue
            message = candidate.get("message") if isinstance(candidate.get("message"), dict) else {}
            role = payload.get("role") or message.get("role")
            content = payload.get("content") or message.get("content") or payload.get("text") or payload.get("last_agent_message")
            usage = payload.get("usage") or message.get("usage") or payload.get("usageMetadata")
            if (role in {"user", "assistant", "model"} and content) or usage:
                return True
    return False


def _files(root: Path) -> list[Path]:
    files: list[Path] = []
    try:
        if root.is_file() and root.suffix.lower() in {".json", ".jsonl"}:
            return [root]
        if root.is_dir():
            files.extend(root.rglob("*.jsonl"))
            files.extend(root.rglob("*.json"))
    except OSError:
        pass
    return list(dict.fromkeys(files))


def index(root: Path) -> list[dict]:
    viewer = _viewer()
    files = _files(default_root())
    entries = []
    for path in files:
        if viewer.is_subagent_path(path):
            continue
        entry = viewer.session_summary(path, "m365_copilot", "external")
        entry["_children"] = [
            viewer.session_summary(child, "m365_copilot", "external")
            for child in viewer.related_subagent_paths(path, files)
        ]
        entries.append(entry)
    for entry in entries:
        entry["_has_data"] = _has_data(entry["_source"])
    return sorted(entries, key=lambda item: item["updated"], reverse=True)


def details(summary: dict) -> dict:
    viewer = _viewer()
    result = viewer.read_external_session(summary["_source"], "m365_copilot")
    result["ownTokens"] = dict(result.get("tokens", {}))
    result["subagents"] = []
    result["subagentTokens"] = viewer.blank_tokens()
    for child_summary in summary.get("_children", []):
        if not isinstance(child_summary, dict) or not isinstance(child_summary.get("_source"), Path):
            continue
        child = viewer.read_external_session(child_summary["_source"], "m365_copilot")
        child["relation"] = "subagent"
        result["subagents"].append(child)
        for key in viewer.TOKEN_KEYS:
            value = child.get("tokens", {}).get(key)
            if isinstance(value, int):
                result["subagentTokens"][key] = (result["subagentTokens"][key] or 0) + value
                result["tokens"][key] = (result["tokens"][key] or 0) + value
    result["source"] = str(summary.get("_source", ""))
    return result


def delete(summary: dict) -> None:
    if summary.get("relation") == "subagent":
        summary["_source"].unlink()
        return
    for child in summary.get("_children", []):
        source = child.get("_source") if isinstance(child, dict) else None
        if isinstance(source, Path):
            source.unlink(missing_ok=True)
    summary["_source"].unlink()


def export_source_files(summary: dict, archive: Path) -> Path:
    files = [(summary["_source"], ".")]
    files.extend(
        (child["_source"], "subagents")
        for child in summary.get("_children", [])
        if isinstance(child, dict) and isinstance(child.get("_source"), Path)
    )
    return create_archive("m365_copilot", archive, files)


def import_source_files(archive: Path, root: Path) -> list[Path]:
    return inject_archive("m365_copilot", archive, default_root() / "imported")