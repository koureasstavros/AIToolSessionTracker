"""Anthropic Claude Code transcript discovery and parsing."""
from pathlib import Path
import json

from .source_archive import create_archive, inject_archive


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


def _is_subagent(path: Path) -> bool:
    return path.parent.name == "subagents" and path.name.startswith("agent-")


def _parent_id(records: list[dict]) -> str | None:
    keys = ("parentSessionId", "parent_session_id", "parentConversationId", "parent_conversation_id")
    for record in records:
        for container in (record, record.get("payload", {}), record.get("message", {})):
            if isinstance(container, dict):
                for key in keys:
                    value = container.get(key)
                    if isinstance(value, str) and value.strip():
                        return value.strip()
    return None


def _children_for(parent: Path, files: list[Path]) -> list[Path]:
    """Find verified subagent siblings without treating every agent as global."""
    children = []
    for path in files:
        if not _is_subagent(path) or path.parent.parent != parent.parent:
            continue
        parent_id = _parent_id(_viewer().safe_json_lines(path))
        if parent_id and parent_id not in {parent.stem, parent.name}:
            continue
        children.append(path)
    # A project directory with one root transcript is a safe fallback for
    # agent files that omit parent metadata.
    roots = [item for item in files if item.parent == parent.parent and not _is_subagent(item)]
    if len(roots) == 1:
        return [path for path in files if _is_subagent(path) and path.parent.parent == parent.parent]
    return children


def index(root: Path) -> list[dict]:
    viewer = _viewer()
    files = _files()
    entries = []
    for path in files:
        if _is_subagent(path):
            continue
        entry = viewer.session_summary(path, "claude", "external")
        children = _children_for(path, files)
        entry["_children"] = [viewer.session_summary(child, "claude", "external") for child in children]
        entries.append(entry)
    for entry in entries:
        entry["_has_data"] = _has_data(entry["_source"])
        records = viewer.safe_json_lines(entry["_source"])
        explicit_name = viewer.explicit_conversation_name(records, entry["id"])
        if explicit_name:
            entry["name"] = explicit_name
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
    tool_calls: dict[str, tuple[dict, dict, dict]] = {}
    message_invocations: dict[str, tuple[dict, dict]] = {}
    seen_usage_records: set[str] = set()
    current_turn: dict | None = None
    previous_was_tool_result = False

    def new_invocation(turn: dict, message_id: str | None) -> dict:
        if message_id and message_id in message_invocations:
            return message_invocations[message_id][1]
        invocation = {
            "index": len(turn.setdefault("invocations", [])) + 1,
            "tokens": viewer.blank_tokens(),
            "tools": [],
            "assistant": [],
        }
        turn["invocations"].append(invocation)
        if message_id:
            message_invocations[message_id] = (turn, invocation)
        return invocation

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
        turn_id = payload.get("turn_id") or payload.get("turnId") or metadata.get("turn_id") or item.get("turn_id") or record.get("promptId")
        content = "\n".join(
            (
                str(part.get("text", ""))
                if isinstance(part, dict)
                else str(part)
            )
            for part in content_parts
            if not (isinstance(part, dict) and part.get("type") in {"tool_use", "tool_result"})
        )

        if role == "user" and not is_tool_result:
            logical_id = str(turn_id or record.get("uuid") or f"turn-{len(turns) + 1}")
            current_turn = turns.setdefault(logical_id, viewer.new_turn(logical_id))
            current_turn["raw"].append(json.dumps(record, indent=2, ensure_ascii=False))
            if content:
                if previous_was_tool_result:
                    viewer.add_internal_instruction(current_turn, "Claude provider instructions", str(content))
                else:
                    current_turn["user"] = str(content)
                    viewer.add_attached_files(current_turn, viewer.files_from_content(raw_content))
                    viewer.add_attached_files(current_turn, viewer.mentioned_files(content))
            previous_was_tool_result = False
            continue

        if is_tool_result:
            for part in content_parts:
                if not isinstance(part, dict) or part.get("type") != "tool_result":
                    continue
                reference = tool_calls.get(str(part.get("tool_use_id")))
                if reference is None:
                    continue
                owner_turn, _, tool = reference
                if not owner_turn["raw"] or owner_turn["raw"][-1] != json.dumps(record, indent=2, ensure_ascii=False):
                    owner_turn["raw"].append(json.dumps(record, indent=2, ensure_ascii=False))
                tool["status"] = "completed"
                tool["result"] = part.get("content", "")
            previous_was_tool_result = True
            continue

        if current_turn is None:
            if role not in {"assistant", "model"}:
                previous_was_tool_result = False
                continue
            logical_id = str(turn_id or record.get("timestamp") or f"turn-{len(turns) + 1}")
            current_turn = turns.setdefault(logical_id, viewer.new_turn(logical_id))

        turn = current_turn
        record_model = viewer.model_from_records([record])
        # Claude writes locally generated API errors as assistant messages
        # whose model is ``<synthetic>``. Preserve that marker on the
        # invocation, but never let it replace a real model used by the turn.
        if record_model and record_model != "<synthetic>":
            turn["model"] = record_model
        turn["raw"].append(json.dumps(record, indent=2, ensure_ascii=False))
        attachment = record.get("attachment") if isinstance(record.get("attachment"), dict) else None
        if attachment:
            viewer.add_attached_files(turn, viewer.files_from_content(attachment))
            attachment_type = str(attachment.get("type") or "internal instructions")
            if attachment_type != "file":
                instruction = json.dumps(attachment, indent=2, ensure_ascii=False)
                viewer.add_internal_instruction(turn, f"Claude {attachment_type}", instruction)
        message_id_value = message.get("id") if isinstance(message, dict) else None
        message_id = str(message_id_value) if message_id_value else None
        invocation = new_invocation(turn, message_id) if role in {"assistant", "model"} or is_tool_use else None
        if invocation is not None and record_model:
            existing_model = invocation.get("model")
            if not existing_model or (existing_model == "<synthetic>" and record_model != "<synthetic>"):
                invocation["model"] = record_model
        if is_tool_use:
            for part in content_parts:
                if isinstance(part, dict) and part.get("type") == "tool_use":
                    tool = {
                        "id": part.get("id"),
                        "name": part.get("name", "unknown"),
                        "arguments": part.get("input", {}),
                        "status": "started",
                    }
                    turn.setdefault("tools", []).append(tool)
                    if invocation is not None:
                        invocation["tools"].append(tool)
                    if part.get("id"):
                        tool_calls[str(part["id"])] = (turn, invocation, tool)
        if content and role in {"assistant", "model"}:
            turn["assistant"].append(str(content))
            if invocation is not None:
                invocation["assistant"].append(str(content))
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
                        if invocation is not None:
                            invocation["tokens"][key] = (invocation["tokens"][key] or 0) + value
        if not result["model"] and payload.get("model"):
            result["model"] = payload["model"]
        previous_was_tool_result = False
    if records and isinstance(records[0], dict):
        first = records[0]
        result["id"] = str(first.get("sessionId") or first.get("session_id") or first.get("conversationId") or first.get("conversation_id") or result["id"])
        result["name"] = viewer.explicit_conversation_name(records, result["id"]) or str(first.get("title") or first.get("name") or first.get("summary") or first.get("conversationTitle") or result["id"])
        if result["name"] == result["id"]:
            result["name"] = viewer.derived_conversation_name(records, result["id"])
    result["turns"] = list(turns.values())
    result["model"] = viewer.model_from_records(records) or result["model"] or "claude"
    result["deployment"] = viewer.deployment_from_records(records)
    for key in viewer.TOKEN_KEYS:
        values = [turn["tokens"][key] for turn in result["turns"] if turn["tokens"][key] is not None]
        if values:
            result["tokens"][key] = sum(values)
    result["source"] = str(summary.get("_source", ""))
    children = summary.get("_children") if isinstance(summary.get("_children"), list) else []
    subagents = []
    subagent_tokens = viewer.blank_tokens()
    for child_summary in children:
        if not isinstance(child_summary, dict) or not isinstance(child_summary.get("_source"), Path):
            continue
        child = viewer.pricing.apply_costs(details(child_summary))
        child["relation"] = "subagent"
        subagents.append(child)
        for key in viewer.TOKEN_KEYS:
            value = child.get("tokens", {}).get(key)
            if isinstance(value, int):
                subagent_tokens[key] = (subagent_tokens[key] or 0) + value
    result["ownTokens"] = dict(result["tokens"])
    result["subagents"] = subagents
    result["subagentTokens"] = subagent_tokens
    for key in viewer.TOKEN_KEYS:
        if subagent_tokens[key] is not None:
            result["tokens"][key] = (result["tokens"][key] or 0) + subagent_tokens[key]
    return result


def delete(summary: dict) -> None:
    source = summary["_source"]
    if summary.get("relation") == "subagent":
        source.unlink()
        return
    for child in summary.get("_children", []):
        child_source = child.get("_source") if isinstance(child, dict) else None
        if isinstance(child_source, Path):
            child_source.unlink(missing_ok=True)
    source.unlink()
    subagents_dir = source.parent / "subagents"
    try:
        if subagents_dir.is_dir() and not any(subagents_dir.iterdir()):
            subagents_dir.rmdir()
    except OSError:
        pass


def export_source_files(summary: dict, archive: Path) -> Path:
    files = [(summary["_source"], ".")]
    for child in summary.get("_children", []):
        source = child.get("_source") if isinstance(child, dict) else None
        if isinstance(source, Path):
            files.append((source, "subagents"))
    return create_archive("claude", archive, files)


def import_source_files(archive: Path, root: Path) -> list[Path]:
    destination = Path.home() / ".claude" / "projects" / "imported"
    return inject_archive("claude", archive, destination)
