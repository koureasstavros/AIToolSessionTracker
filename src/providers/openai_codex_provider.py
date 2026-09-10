"""OpenAI Codex transcript discovery and parsing."""
from pathlib import Path
import json
import re

from src.common.source_archive import create_archive, inject_archive

UUID_PATTERN = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)


def _viewer():
    import session_token_viewer as viewer
    return viewer


def display_root(root: Path) -> Path:
    return Path.home() / ".codex" / "sessions"


def tool(summary: dict) -> str:
    return str(summary.get("_surface") or "Mixed")


def _surface_from_records(records: list[dict]) -> str:
    """Identify Codex's CLI, VS Code, and Desktop origin when recorded."""
    surfaces: set[str] = set()
    for record in records:
        payload = record.get("payload", record)
        if not isinstance(payload, dict):
            continue
        originator = str(payload.get("originator") or record.get("originator") or "").lower()
        source = str(payload.get("source") or record.get("source") or "").lower()
        marker = originator or source
        if "desktop" in marker:
            surfaces.add("Desktop")
        elif "vscode" in marker or marker == "vscode" or "extension" in marker:
            surfaces.add("Extension")
        elif "cli" in marker or marker in {"codex-tui", "tui"}:
            surfaces.add("CLI")
    if len(surfaces) == 1:
        return next(iter(surfaces))
    return "Mixed"


def identity(record: dict, fallback: str) -> tuple[str, str]:
    raw_id = str(record.get("sessionId") or record.get("session_id") or fallback)
    match = UUID_PATTERN.search(raw_id)
    session_id = match.group(0) if match else raw_id
    name = next(
        (str(record[key]) for key in ("title", "name", "summary", "session_name")
         if record.get(key) and not str(record[key]).startswith("rollout-")),
        session_id,
    )
    return session_id, name


def _has_data(path: Path) -> bool:
    for record in _viewer().safe_json_lines(path):
        payload = record.get("payload", record)
        if not isinstance(payload, dict):
            continue
        message = record.get("message") if isinstance(record.get("message"), dict) else {}
        item = payload.get("item") if isinstance(payload.get("item"), dict) else {}
        role = payload.get("role") or message.get("role") or item.get("role")
        content = payload.get("content") or message.get("content") or item.get("content") or payload.get("text") or item.get("text")
        info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
        usage = payload.get("usage") or message.get("usage") or payload.get("usageMetadata") or info.get("last_token_usage")
        if (role in {"user", "assistant", "model"} and content) or usage:
            return True
    return False


def index(root: Path) -> list[dict]:
    viewer = _viewer()
    location = Path.home() / ".codex" / "sessions"
    try:
        files = list(location.rglob("*.jsonl")) if location.exists() else []
    except OSError:
        files = []
    entries = []
    for path in files:
        if viewer.is_subagent_path(path):
            continue
        entry = viewer.session_summary(path, "codex", "external")
        entry["_children"] = [
            viewer.session_summary(child, "codex", "external")
            for child in viewer.related_subagent_paths(path, files)
        ]
        entries.append(entry)
    for entry in entries:
        entry["_has_data"] = _has_data(entry["_source"])
        entry["_surface"] = _surface_from_records(viewer.safe_json_lines(entry["_source"]))
        entry["_source_label"] = tool(entry)
    return entries


def details(summary: dict) -> dict:
    viewer = _viewer()
    path = summary["_source"]
    try:
        updated = path.stat().st_mtime
    except OSError:
        updated = 0
    result = viewer.new_session(path.stem, path.stem, updated)
    records = viewer.safe_json_lines(path)
    result["project"] = viewer.project_from_records(records)
    turns: dict[str, dict] = {}
    tool_calls: dict[str, tuple[dict, dict]] = {}
    current_turn_id = ""
    current_invocation: dict | None = None
    session_context_blocks = viewer.context_instruction_blocks(records, "Codex")
    session_instructions = ""
    if records and isinstance(records[0].get("payload"), dict):
        base = records[0]["payload"].get("base_instructions")
        if isinstance(base, dict):
            session_instructions = str(base.get("text") or "")
        elif isinstance(base, str):
            session_instructions = base

    def get_turn(turn_id: str | None = None) -> dict:
        nonlocal current_turn_id
        current_turn_id = str(turn_id or current_turn_id or f"turn-{len(turns) + 1}")
        turn = turns.setdefault(current_turn_id, viewer.new_turn(current_turn_id))
        if session_instructions and len(turns) == 1:
            viewer.add_internal_instruction(turn, "Codex base instructions", session_instructions)
        if len(turns) == 1:
            for block_name, block_content in session_context_blocks:
                viewer.add_internal_instruction(turn, block_name, block_content)
        return turn

    def get_invocation(turn: dict) -> dict:
        nonlocal current_invocation
        if current_invocation is None:
            current_invocation = {
                "index": len(turn.setdefault("invocations", [])) + 1,
                "tokens": viewer.blank_tokens(),
                "tools": [],
                "assistant": [],
            }
            turn["invocations"].append(current_invocation)
        return current_invocation

    for record in records:
        payload = record.get("payload", record)
        if not isinstance(payload, dict):
            continue
        item = payload.get("item") if isinstance(payload.get("item"), dict) else {}
        message = record.get("message") if isinstance(record.get("message"), dict) else {}
        metadata = payload.get("internal_chat_message_metadata_passthrough")
        if not isinstance(metadata, dict):
            metadata = {}
        turn_id = (payload.get("turn_id") or payload.get("turnId") or item.get("turn_id")
                   or metadata.get("turn_id") or record.get("promptId"))
        event_type = record.get("type")
        payload_type = payload.get("type")
        if event_type in {"event_msg", "turn_context"} and payload_type in {"task_started", "task_complete"}:
            turn_id = turn_id or payload.get("turn_id")
        if turn_id and (payload_type == "task_started" or event_type == "turn_context"):
            current_turn_id = str(turn_id)
            current_invocation = None
        if not turn_id and record.get("type") == "user":
            turn_id = record.get("uuid")
        if not turn_id and not current_turn_id and record.get("type") in {"queue-operation", "attachment"}:
            continue
        call_id = str(payload.get("call_id") or payload.get("callId") or "")
        role = payload.get("role") or message.get("role") or item.get("role")
        relevant = (
            role in {"user", "assistant", "model"}
            or payload_type in {"function_call", "function_call_output", "token_count", "reasoning", "task_started", "task_complete"}
            or event_type == "turn_context"
        )
        if not relevant or (not current_turn_id and not turn_id):
            continue
        turn = get_turn(str(turn_id) if turn_id else None)
        turn_model = viewer.model_from_records([record])
        if turn_model:
            turn["model"] = turn_model
        turn["raw"].append(json.dumps(record, indent=2, ensure_ascii=False))
        info = payload.get("info") or {}
        usage = payload.get("usage") or message.get("usage") or payload.get("usageMetadata") or (info.get("last_token_usage") if isinstance(info, dict) else {})
        normalized_usage = viewer.usage_from(usage) if isinstance(usage, dict) else viewer.blank_tokens()
        has_usage = any(value is not None for value in normalized_usage.values())
        creates_invocation = (
            payload_type in {"function_call", "function_call_output", "reasoning"}
            or role in {"assistant", "model"}
            or (payload_type == "token_count" and has_usage)
        )
        invocation = get_invocation(turn) if creates_invocation else None
        if payload_type == "function_call":
            arguments = payload.get("arguments", payload.get("input", {}))
            tool = {
                    "id": call_id,
                    "name": payload.get("name", "unknown"),
                    "arguments": arguments,
                    "status": "started",
            }
            turn.setdefault("tools", []).append(tool)
            if invocation is not None:
                invocation["tools"].append(tool)
            tool_calls[call_id] = (turn, tool)
        elif payload_type == "function_call_output":
            owner_turn, tool = tool_calls.get(call_id, (turn, None))
            if tool is None:
                tool = {"id": call_id, "name": "unknown", "status": "started"}
                owner_turn.setdefault("tools", []).append(tool)
                if invocation is not None:
                    invocation["tools"].append(tool)
            tool["status"] = "completed"
            tool["result"] = payload.get("output", payload.get("result", ""))
        content = payload.get("content") or message.get("content") or item.get("content") or payload.get("text") or item.get("text")
        if isinstance(content, list):
            content = "\n".join(str(part.get("text", part)) if isinstance(part, dict) else str(part) for part in content)
        if content and role == "user":
            turn["user"] = str(content)
            viewer.add_attached_files(turn, viewer.mentioned_files(content))
        elif content and role in {"assistant", "model"}:
            turn["assistant"].append(str(content))
            if invocation is not None:
                invocation["assistant"].append(str(content))
        for key, value in normalized_usage.items():
            if value is not None:
                turn["tokens"][key] = (turn["tokens"][key] or 0) + value
                if invocation is not None:
                    invocation["tokens"][key] = (invocation["tokens"][key] or 0) + value
        if payload_type == "token_count" and has_usage:
            # A token_count closes one model invocation. The next assistant
            # message or tool batch begins a new invocation in this turn.
            current_invocation = None
        elif payload_type == "task_complete":
            current_invocation = None
            current_turn_id = ""
        if not result["model"] and payload.get("model"):
            result["model"] = payload["model"]
    if records and isinstance(records[0], dict):
        first = records[0]
        result["id"], result["name"] = identity(first, result["id"])
        if result["name"] == result["id"]:
            result["name"] = viewer.derived_conversation_name(records, result["id"])
    result["turns"] = list(turns.values())
    result["model"] = result["model"] or "codex"
    result["deployment"] = viewer.deployment_from_records(records)
    for key in viewer.TOKEN_KEYS:
        values = [turn["tokens"][key] for turn in result["turns"] if turn["tokens"][key] is not None]
        if values:
            result["tokens"][key] = sum(values)
    viewer.subtract_cached_input(result)
    children = summary.get("_children") if isinstance(summary.get("_children"), list) else []
    result["subagents"] = []
    result["ownTokens"] = dict(result["tokens"])
    result["subagentTokens"] = viewer.blank_tokens()
    for child_summary in children:
        if not isinstance(child_summary, dict) or not isinstance(child_summary.get("_source"), Path):
            continue
        child = details(child_summary)
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
    return create_archive("codex", archive, files)


def import_source_files(archive: Path, root: Path) -> list[Path]:
    return inject_archive("codex", archive, display_root(root) / "imported")
