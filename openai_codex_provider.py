"""OpenAI Codex transcript discovery and parsing."""
from pathlib import Path
import json
import re

UUID_PATTERN = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)


def _viewer():
    import session_token_viewer as viewer
    return viewer


def display_root(root: Path) -> Path:
    return Path.home() / ".codex" / "sessions"


def tool(summary: dict) -> str:
    return "CLI"


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
    entries = [viewer.session_summary(path, "codex", "external") for path in files]
    for entry in entries:
        entry["_has_data"] = _has_data(entry["_source"])
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
    tool_turns: dict[str, str] = {}
    current = ""
    for record in records:
        payload = record.get("payload", record)
        if not isinstance(payload, dict):
            continue
        item = payload.get("item") if isinstance(payload.get("item"), dict) else {}
        message = record.get("message") if isinstance(record.get("message"), dict) else {}
        turn_id = payload.get("turn_id") or payload.get("turnId") or item.get("turn_id") or record.get("promptId")
        if not turn_id and record.get("type") == "user":
            turn_id = record.get("uuid")
        if not turn_id and not current and record.get("type") in {"queue-operation", "attachment"}:
            continue
        payload_type = payload.get("type")
        call_id = str(payload.get("call_id") or payload.get("callId") or "")
        if payload_type == "function_call":
            current = f"{call_id or turn_id or len(turns)}:tool:{len(turns)}"
            tool_turns[call_id] = current
        elif payload_type == "function_call_output" and call_id in tool_turns:
            current = tool_turns[call_id]
        else:
            current = str(turn_id or current or record.get("timestamp") or len(turns))
        turn = turns.setdefault(current, viewer.new_turn(current))
        turn["raw"].append(json.dumps(record, indent=2, ensure_ascii=False))
        if payload_type in {"function_call", "function_call_output"}:
            turn["kind"] = "tool"
            if payload_type == "function_call":
                arguments = payload.get("arguments", payload.get("input", {}))
                turn["assistant"].append(
                    f"Tool: {payload.get('name', 'unknown')}\nInput: "
                    f"{arguments if isinstance(arguments, str) else json.dumps(arguments, ensure_ascii=False)}"
                )
            else:
                output = payload.get("output", payload.get("result", ""))
                if isinstance(output, (dict, list)):
                    output = json.dumps(output, ensure_ascii=False)
                if output:
                    turn["assistant"].append(f"Tool output:\n{output}")
        role = payload.get("role") or message.get("role") or item.get("role")
        content = payload.get("content") or message.get("content") or item.get("content") or payload.get("text") or item.get("text")
        if isinstance(content, list):
            content = "\n".join(str(part.get("text", part)) if isinstance(part, dict) else str(part) for part in content)
        if content and role == "user":
            turn["user"] = str(content)
        elif content and role in {"assistant", "model"}:
            turn["assistant"].append(str(content))
        info = payload.get("info") or {}
        usage = payload.get("usage") or message.get("usage") or payload.get("usageMetadata") or (info.get("last_token_usage") if isinstance(info, dict) else {})
        if isinstance(usage, dict):
            for key, value in viewer.usage_from(usage).items():
                if value is not None:
                    turn["tokens"][key] = (turn["tokens"][key] or 0) + value
        if not result["model"] and payload.get("model"):
            result["model"] = payload["model"]
    if records and isinstance(records[0], dict):
        first = records[0]
        result["id"], result["name"] = identity(first, result["id"])
        if result["name"] == result["id"]:
            result["name"] = viewer.derived_conversation_name(records, result["id"])
    result["turns"] = list(turns.values())
    result["model"] = result["model"] or "codex"
    for key in viewer.TOKEN_KEYS:
        values = [turn["tokens"][key] for turn in result["turns"] if turn["tokens"][key] is not None]
        if values:
            result["tokens"][key] = sum(values)
    viewer.subtract_cached_input(result)
    result["source"] = str(summary.get("_source", ""))
    return result


def delete(summary: dict) -> None:
    summary["_source"].unlink()
