"""Anthropic Claude Code transcript discovery and parsing."""
from pathlib import Path
import json


def _viewer():
    import session_token_viewer as viewer
    return viewer


def display_root(root: Path) -> Path:
    return Path.home() / ".claude" / "sessions"


def tool(summary: dict) -> str:
    # Claude Code CLI and the VS Code integration can share the same
    # ~/.claude transcript locations. Only Desktop has a distinct local-agent
    # storage path, so expose both possible labels when the source is shared.
    return "Desktop" if "local-agent-mode-sessions" in str(summary.get("_source", "")) else "CLI / Extension"


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
            author = payload.get("author") if isinstance(payload.get("author"), dict) else {}
            role = payload.get("role") or message.get("role") or author.get("role")
            content = payload.get("content") or message.get("content") or payload.get("text") or payload.get("last_agent_message")
            usage = payload.get("usage") or message.get("usage") or payload.get("usageMetadata")
            if (role in {"user", "assistant", "model"} and content) or usage:
                return True
    return False


def _files() -> list[Path]:
    locations = (
        (Path.home() / ".claude" / "sessions", "*.json"),
        (Path.home() / ".claude" / "projects", "*.jsonl"),
        (Path.home() / "AppData" / "Local" / "Claude-3p" / "local-agent-mode-sessions", "audit.jsonl"),
    )
    files: list[Path] = []
    for location, pattern in locations:
        try:
            if location.exists():
                files.extend(location.glob(pattern) if pattern == "*.json" else location.rglob(pattern))
        except OSError:
            continue
    return list(dict.fromkeys(files))


def index(root: Path) -> list[dict]:
    viewer = _viewer()
    entries = [viewer.session_summary(path, "claude", "external") for path in _files()]
    for entry in entries:
        entry["_has_data"] = _has_data(entry["_source"])
        # Keep the sidebar flag consistent with the surface shown in the
        # conversation header. Shared .claude paths intentionally retain both
        # possible labels.
        entry["_source_label"] = tool(entry)
    # A conversation can be discovered through more than one local Claude
    # source while it is being written. Keep one sidebar item per session ID.
    unique: dict[str, dict] = {}
    for entry in entries:
        existing = unique.get(entry["id"])
        if existing is None or (
            entry.get("_has_data") and not existing.get("_has_data")
        ) or entry["updated"] > existing["updated"]:
            unique[entry["id"]] = entry
    return sorted(unique.values(), key=lambda item: item["updated"], reverse=True)


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
    if result["project"] and "local-agent-mode-sessions" in result["project"].replace("/", "\\").lower():
        result["project"] = None
    turns: dict[str, dict] = {}
    tool_turns: dict[str, str] = {}
    message_turns: dict[str, str] = {}
    seen_usage_records: set[str] = set()
    current = ""
    for record in records:
        payload = record.get("payload", record)
        if not isinstance(payload, dict):
            continue
        item = payload.get("item") if isinstance(payload.get("item"), dict) else {}
        message = record.get("message") if isinstance(record.get("message"), dict) else {}
        metadata = payload.get("internal_chat_message_metadata_passthrough") or item.get("internal_chat_message_metadata_passthrough") or {}
        if not isinstance(metadata, dict):
            metadata = {}
        author = payload.get("author") or message.get("author") or item.get("author") or {}
        role = payload.get("role") or message.get("role") or item.get("role")
        if isinstance(author, dict):
            role = role or author.get("role")
        raw_content = (payload.get("content") or message.get("content") or item.get("content")
                       or payload.get("text") or item.get("text") or payload.get("last_agent_message"))
        content_parts = raw_content if isinstance(raw_content, list) else ([] if raw_content is None else [raw_content])
        is_tool_result = bool(content_parts) and all(
            isinstance(part, dict) and part.get("type") == "tool_result"
            for part in content_parts
        )
        is_tool_use = any(
            isinstance(part, dict) and part.get("type") == "tool_use"
            for part in content_parts
        )
        if is_tool_result and role == "user":
            # Claude serializes tool results as user-role messages. They are
            # not new user prompts and should not appear as JSON input.
            role = None
        turn_id = payload.get("turn_id") or payload.get("turnId") or metadata.get("turn_id") or item.get("turn_id") or record.get("promptId")
        if not turn_id and record.get("type") == "user":
            turn_id = record.get("uuid")
        if not turn_id and not current and (payload.get("type") or record.get("type")) in {"session_meta", "world_state", "turn_context", "queue-operation", "attachment"}:
            continue
        tool_id = next(
            (part.get("tool_use_id") for part in content_parts
             if isinstance(part, dict) and part.get("type") == "tool_result" and part.get("tool_use_id")),
            None,
        )
        mapped_tool_turn = tool_turns.get(str(tool_id)) if tool_id else None
        message_id = message.get("id") if isinstance(message, dict) else None
        mapped_message_turn = message_turns.get(str(message_id)) if message_id else None
        next_turn = str(mapped_tool_turn or mapped_message_turn or turn_id or current or record.get("timestamp") or len(turns))
        if not mapped_tool_turn and not mapped_message_turn and role in {"assistant", "model"} and current in turns:
            previous = turns[current]
            # Text and tool_use records with the same message ID are one API
            # response. A new assistant message starts the next turn so its
            # usage stays with the content represented by that usage.
            if previous["assistant"] or previous.get("tools"):
                next_turn = f"{next_turn}:assistant:{len(turns)}"
        if message_id and not mapped_tool_turn:
            message_turns[str(message_id)] = next_turn
        current = next_turn
        turn = turns.setdefault(current, viewer.new_turn(current))
        turn["raw"].append(json.dumps(record, indent=2, ensure_ascii=False))
        content = "\n".join(
            (
                str(part.get("text", ""))
                if isinstance(part, dict)
                else str(part)
            )
            for part in content_parts
            if not (isinstance(part, dict) and part.get("type") in {"tool_use", "tool_result"})
        )
        if is_tool_use:
            turn["kind"] = "tool"
            for part in content_parts:
                if isinstance(part, dict) and part.get("type") == "tool_use":
                    turn.setdefault("tools", []).append({
                        "id": part.get("id"),
                        "name": part.get("name", "unknown"),
                        "arguments": part.get("input", {}),
                        "status": "started",
                    })
                    if part.get("id"):
                        tool_turns[str(part["id"])] = next_turn
        if is_tool_result:
            turn["kind"] = "tool"
            for part in content_parts:
                if isinstance(part, dict) and part.get("type") == "tool_result":
                    tools = turn.setdefault("tools", [])
                    tool = next((entry for entry in tools if entry.get("id") == part.get("tool_use_id")), None)
                    if tool is None:
                        tool = {"id": part.get("tool_use_id"), "name": "unknown"}
                        tools.append(tool)
                    tool["status"] = "completed"
                    tool["result"] = part.get("content", "")
        if content and role == "user":
            turn["user"] = str(content)
        elif content and role in {"assistant", "model"}:
            turn["assistant"].append(str(content))
        elif content and is_tool_result:
            turn["assistant"].append(str(content))
        info = payload.get("info") or {}
        usage = payload.get("usage") or message.get("usage") or payload.get("usageMetadata") or (info.get("last_token_usage") if isinstance(info, dict) else {})
        if isinstance(usage, dict):
            # Claude Code can persist one assistant API response as multiple
            # records: for example, a text record followed by a tool_use
            # record. Both records carry the same message ID and usage. Count
            # that usage once, otherwise tool calls inflate the totals.
            usage_id = message_id or record.get("requestId")
            if usage_id is None:
                usage_id = record.get("uuid")
            if usage_id is None or str(usage_id) not in seen_usage_records:
                if usage_id is not None:
                    seen_usage_records.add(str(usage_id))
                for key, value in viewer.usage_from(usage).items():
                    if value is not None:
                        turn["tokens"][key] = (turn["tokens"][key] or 0) + value
        if not result["model"] and payload.get("model"):
            result["model"] = payload["model"]
    if records and isinstance(records[0], dict):
        first = records[0]
        result["id"] = str(first.get("sessionId") or first.get("session_id") or first.get("conversationId") or first.get("conversation_id") or result["id"])
        result["name"] = str(first.get("title") or first.get("name") or first.get("summary") or first.get("conversationTitle") or result["id"])
        if result["name"] == result["id"]:
            result["name"] = viewer.derived_conversation_name(records, result["id"])
    result["turns"] = list(turns.values())
    result["model"] = result["model"] or "claude"
    for key in viewer.TOKEN_KEYS:
        values = [turn["tokens"][key] for turn in result["turns"] if turn["tokens"][key] is not None]
        if values:
            result["tokens"][key] = sum(values)
    result["source"] = str(summary.get("_source", ""))
    return result


def delete(summary: dict) -> None:
    summary["_source"].unlink()
