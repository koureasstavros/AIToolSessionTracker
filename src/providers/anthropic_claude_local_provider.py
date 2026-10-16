"""Anthropic Claude Code transcript discovery and parsing."""
from pathlib import Path
import json
import re

from src.common.source_paths import claude_desktop_roots
from src.common.source_archive import create_archive, inject_archive


def _viewer():
    import session_token_viewer as viewer
    return viewer


TOKEN_ALIASES = {
    "inputTokens": ("input_tokens", "prompt_tokens", "promptTokens", "inputTokens"),
    "cacheReadTokens": ("cached_tokens", "cachedTokens", "input_cached_tokens", "cached_input_tokens", "cache_read_input_tokens", "cacheReadTokens"),
    "cacheWriteTokens": ("cache_write_input_tokens", "input_cache_write_tokens", "cache_creation_input_tokens", "cacheWriteTokens"),
    "outputTokens": ("output_tokens", "completion_tokens", "outputTokens"),
    "reasoningTokens": ("reasoning_output_tokens", "reasoning_tokens", "thinking_tokens", "thinkingTokens", "reasoningTokens"),
}
CLAUDE_CONTEXT_TAGS = {
    "ide_selection",
    "ide_opened_file",
    "system-reminder",
    "environment_context",
    "local-command-caveat",
    "local-command-stdout",
}


def _split_claude_context(content: str) -> tuple[str, list[tuple[str, str]]]:
    """Separate allowlisted Claude context tags from the human prompt."""
    pattern = re.compile(
        r"<(?P<tag>[A-Za-z][A-Za-z0-9_-]*)>(?P<body>.*?)</(?P=tag)>",
        re.DOTALL | re.IGNORECASE,
    )
    context: list[tuple[str, str]] = []

    def replace(match: re.Match[str]) -> str:
        tag = match.group("tag").lower()
        body = match.group("body").strip()
        if tag not in CLAUDE_CONTEXT_TAGS or not body:
            return match.group(0)
        context.append((tag, body))
        return ""

    return pattern.sub(replace, content).strip(), context


def _split_ide_selection(content: str) -> tuple[str, list[str]]:
    """Backward-compatible helper for callers interested only in selections."""
    prompt, context = _split_claude_context(content)
    return prompt, [body for tag, body in context if tag == "ide_selection"]


def _clean_title(value: object, fallback: str) -> str:
    """Remove provider context blocks before using a Claude title."""
    text = str(value or "").strip()
    opening = re.match(r"<([A-Za-z][A-Za-z0-9_-]*)>", text)
    if opening and opening.group(1).lower() in CLAUDE_CONTEXT_TAGS and not re.search(
        rf"</{re.escape(opening.group(1))}>", text, re.IGNORECASE
    ):
        return fallback
    prompt, _ = _split_claude_context(text)
    if prompt.strip():
        return prompt.strip()
    if opening and opening.group(1).lower() in CLAUDE_CONTEXT_TAGS:
        return fallback
    return text or fallback


def _user_prompt_title(records: list[dict], fallback: str) -> str:
    """Derive a title from user text after removing provider context blocks."""
    for record in records:
        message = record.get("message") if isinstance(record.get("message"), dict) else {}
        payload = record.get("payload") if isinstance(record.get("payload"), dict) else record
        role = message.get("role") or payload.get("role") or record.get("role")
        if role != "user":
            continue
        content = message.get("content") or payload.get("content") or payload.get("text")
        parts = content if isinstance(content, list) else [content]
        text = "\n".join(
            str(part.get("text", "")) if isinstance(part, dict) else str(part or "")
            for part in parts
        )
        prompt, _ = _split_claude_context(text)
        prompt = _viewer().clean_session_prompt(prompt)
        if prompt:
            return prompt[:77].rstrip() + "..." if len(prompt) > 80 else prompt
    return fallback


def normalize_usage(source: dict) -> dict:
    """Normalize Claude usage aliases to the viewer's canonical fields."""
    normalized = {}
    for key, aliases in TOKEN_ALIASES.items():
        for alias in aliases:
            if alias in source:
                normalized[key] = source[alias]
                break
    output_details = source.get("output_tokens_details")
    if isinstance(output_details, dict) and "reasoningTokens" not in normalized:
        for alias in TOKEN_ALIASES["reasoningTokens"]:
            if alias in output_details:
                normalized["reasoningTokens"] = output_details[alias]
                break
    cache_creation = source.get("cache_creation")
    if isinstance(cache_creation, dict):
        normalized["cacheWrite5mTokens"] = cache_creation.get("ephemeral_5m_input_tokens")
        normalized["cacheWrite1hTokens"] = cache_creation.get("ephemeral_1h_input_tokens")
        normalized.setdefault("cacheWriteTokens", cache_creation.get("input_tokens"))
    return normalized


def usage_from(source: dict) -> dict:
    return _viewer().usage_from(normalize_usage(source))


def token_fields_from_sources(*sources: object) -> list[str]:
    return _viewer().token_fields_from_sources(*(normalize_usage(source) for source in sources if isinstance(source, dict)))


def display_root(root: Path) -> Path:
    return Path.home() / ".claude" / "sessions"


def tool(summary: dict) -> str:
    surface = summary.get("_surface")
    if isinstance(surface, str) and surface:
        return surface
    # Shared .claude transcript paths do not normally identify their surface.
    return "Desktop" if "local-agent-mode-sessions" in str(summary.get("_source", "")) else "Mixed"


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
        *((root, "audit.jsonl") for root in claude_desktop_roots()),
    )
    files: list[Path] = []
    for location, pattern in locations:
        try:
            if location.exists():
                files.extend(location.glob(pattern) if pattern == "*.json" else location.rglob(pattern))
        except OSError:
            continue
    return list(dict.fromkeys(files))


def _surface_by_session_id(files: list[Path]) -> dict[str, set[str]]:
    """Collect explicit Desktop and entrypoint markers for shared transcripts."""
    surfaces: dict[str, set[str]] = {}

    def add_surface(session_id: object, surface: str) -> None:
        if session_id:
            surfaces.setdefault(str(session_id), set()).add(surface)

    projects = Path.home() / ".claude" / "projects"
    try:
        for marker in projects.rglob("*.desktop-released.json") if projects.exists() else ():
            session_id = marker.name.removesuffix(".desktop-released.json")
            add_surface(session_id, "Desktop")
    except OSError:
        pass

    for path in files:
        records = _viewer().safe_json_lines(path)
        for record in records:
            session_id = record.get("sessionId") or record.get("session_id")
            entrypoint = str(record.get("entrypoint") or "").lower()
            if not session_id or not entrypoint:
                continue
            if "desktop" in entrypoint:
                add_surface(session_id, "Desktop")
            elif "vscode" in entrypoint or "extension" in entrypoint:
                add_surface(session_id, "Extension")
            elif "cli" in entrypoint:
                add_surface(session_id, "CLI")
    return surfaces


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
    """Find direct subagents for a transcript, including nested agent levels."""
    child_directories = {parent.parent / parent.stem / "subagents"}
    # Older Claude layouts stored the root's agents directly beside the root
    # transcript. Keep accepting that form while using the transcript-specific
    # directory for nested agents.
    if parent.parent.name != "subagents":
        child_directories.add(parent.parent / "subagents")
    children = []
    for path in files:
        if not _is_subagent(path) or path.parent not in child_directories:
            continue
        parent_id = _parent_id(_viewer().safe_json_lines(path))
        if parent_id and parent_id not in {parent.stem, parent.name}:
            continue
        children.append(path)
    return children


def _summary_with_children(path: Path, files: list[Path]) -> dict:
    """Build a summary tree while preserving each agent's own transcript."""
    summary = _viewer().session_summary(path, "anthropic_claude", "external")
    metadata_path = path.with_suffix(".meta.json")
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        metadata = {}
    if isinstance(metadata, dict):
        summary["_agent_meta"] = metadata
    summary["_children"] = [
        _summary_with_children(child, files)
        for child in _children_for(path, files)
    ]
    return summary


def index(root: Path) -> list[dict]:
    viewer = _viewer()
    files = _files()
    surfaces = _surface_by_session_id(files)
    entries = []
    for path in files:
        if _is_subagent(path):
            continue
        entry = viewer.session_summary(path, "anthropic_claude", "external")
        children = _children_for(path, files)
        entry["_children"] = [_summary_with_children(child, files) for child in children]
        entries.append(entry)
    for entry in entries:
        entry["_has_data"] = _has_data(entry["_source"])
        records = viewer.safe_json_lines(entry["_source"])
        explicit_name = viewer.explicit_conversation_name(records, entry["id"])
        if explicit_name:
            entry["name"] = _clean_title(explicit_name, entry["id"])
        else:
            entry["name"] = _clean_title(entry.get("name"), entry["id"])
        if entry["name"] == entry["id"]:
            entry["name"] = _user_prompt_title(records, entry["id"])
        source_path = str(entry["_source"]).replace("\\", "/")
        if "local-agent-mode-sessions" in source_path:
            entry["_surface"] = "Desktop"
        elif entry["id"] in surfaces:
            # A session can be observed by more than one Claude surface. Mark
            # that case as Mixed instead of presenting a misleading combined
            # source label.
            evidence = surfaces[entry["id"]]
            entry["_surface"] = (
                "Mixed" if len(evidence) > 1 else next(iter(evidence))
            )
        # Keep the sidebar flag consistent with the surface shown in the
        # conversation header. Shared .claude paths retain an explicit marker
        # when one is available and otherwise remain ambiguous.
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
    result["reasoningEffort"] = viewer.session_reasoning_effort_from_records(records)
    if result["project"] and "local-agent-mode-sessions" in result["project"].replace("/", "\\").lower():
        result["project"] = None
    turns: dict[str, dict] = {}
    tool_calls: dict[str, tuple[dict, dict, dict]] = {}
    message_invocations: dict[str, tuple[dict, dict]] = {}
    # Claude can emit a partial assistant record and a later final record
    # with the same message ID. Keep the latest normalized usage so final
    # output and thinking-token counts replace the partial values instead of
    # being discarded by message-level deduplication.
    seen_usage_records: dict[str, dict[str, int | None]] = {}
    session_token_fields: set[str] = set()
    current_turn: dict | None = None
    previous_was_tool_result = False
    pending_instructions: list[tuple[str, str]] = []

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

    def capture_attachment(target: dict | None, record: dict) -> None:
        attachment = record.get("attachment") if isinstance(record.get("attachment"), dict) else None
        if not attachment:
            return
        if target is None:
            attachment_type = str(attachment.get("type") or "internal instructions")
            if attachment_type != "file":
                instruction = json.dumps(attachment, indent=2, ensure_ascii=False)
                pending_instructions.append((f"Claude {attachment_type}", instruction))
            return
        viewer.add_attached_files(target, viewer.files_from_content(attachment))
        attachment_type = str(attachment.get("type") or "internal instructions")
        if attachment_type != "file":
            instruction = json.dumps(attachment, indent=2, ensure_ascii=False)
            viewer.add_internal_instruction(target, f"Claude {attachment_type}", instruction)

    def apply_pending_instructions(target: dict) -> None:
        for name, content in pending_instructions:
            viewer.add_internal_instruction(target, name, content)
        pending_instructions.clear()

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

        capture_attachment(current_turn, record)

        task_notification = role == "user" and "<task-notification>" in content
        if task_notification:
            tool_id_match = re.search(r"<tool-use-id>(.*?)</tool-use-id>", content, re.DOTALL)
            result_match = re.search(r"<result>(.*?)</result>", content, re.DOTALL)
            tool_id = tool_id_match.group(1).strip() if tool_id_match else ""
            reference = tool_calls.get(tool_id)
            if reference is not None:
                owner_turn, _, tool = reference
                current_turn = owner_turn
                serialized = json.dumps(record, indent=2, ensure_ascii=False)
                if not owner_turn["raw"] or owner_turn["raw"][-1] != serialized:
                    owner_turn["raw"].append(serialized)
                tool["status"] = "completed"
                if result_match:
                    tool["result"] = result_match.group(1).strip()
            elif current_turn is not None:
                current_turn["raw"].append(json.dumps(record, indent=2, ensure_ascii=False))
            previous_was_tool_result = True
            continue

        if role == "user" and not is_tool_result:
            logical_id = str(turn_id or record.get("uuid") or f"turn-{len(turns) + 1}")
            current_turn = turns.setdefault(logical_id, viewer.new_turn(logical_id))
            apply_pending_instructions(current_turn)
            current_turn["raw"].append(json.dumps(record, indent=2, ensure_ascii=False))
            if content:
                if previous_was_tool_result:
                    viewer.add_internal_instruction(current_turn, "Claude provider instructions", str(content))
                else:
                    prompt, context = _split_claude_context(str(content))
                    for tag, body in context:
                        label = "Claude IDE selection" if tag == "ide_selection" else f"Claude {tag} context"
                        viewer.add_internal_instruction(current_turn, label, body)
                    current_turn["user"] = prompt
                    viewer.add_attached_files(current_turn, viewer.files_from_content(raw_content))
                    viewer.add_attached_files(current_turn, viewer.mentioned_files(prompt))
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
        effort = viewer.reasoning_effort_from_record(record)
        if effort:
            turn["reasoningEffort"] = effort
        record_model = viewer.model_from_records([record])
        # Claude writes locally generated API errors as assistant messages
        # whose model is ``<synthetic>``. Preserve that marker on the
        # invocation, but never let it replace a real model used by the turn.
        if record_model and record_model != "<synthetic>":
            turn["model"] = record_model
        turn["raw"].append(json.dumps(record, indent=2, ensure_ascii=False))
        message_id_value = message.get("id") if isinstance(message, dict) else None
        message_id = str(message_id_value) if message_id_value else None
        invocation = new_invocation(turn, message_id) if role in {"assistant", "model"} or is_tool_use else None
        if invocation is not None and effort:
            invocation["reasoningEffort"] = effort
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
            usage_fields = token_fields_from_sources(usage)
            session_token_fields.update(usage_fields)
            turn.setdefault("tokenFields", [])
            turn["tokenFields"] = list(set(turn["tokenFields"]) | set(usage_fields))
            if invocation is not None:
                invocation.setdefault("tokenFields", [])
                invocation["tokenFields"] = list(set(invocation["tokenFields"]) | set(usage_fields))
            # Claude Code can persist one assistant API response as multiple
            # records: for example, a text record followed by a tool_use
            # record. Both records carry the same message ID and usage. Count
            # that usage once, otherwise tool calls inflate the totals.
            usage_id = message_id or record.get("requestId")
            if usage_id is None:
                usage_id = record.get("uuid")
            normalized_usage = usage_from(usage)
            if usage_id is None:
                usage_delta = normalized_usage
            else:
                usage_key = str(usage_id)
                previous_usage = seen_usage_records.get(usage_key, viewer.blank_tokens())
                usage_delta = {
                    key: (normalized_usage.get(key) or 0) - (previous_usage.get(key) or 0)
                    for key in viewer.CANONICAL_TOKEN_KEYS
                }
                seen_usage_records[usage_key] = normalized_usage
            for key, value in usage_delta.items():
                if value is not None:
                    if key in viewer.CANONICAL_CACHE_WRITE_TOKEN_KEYS and normalized_usage.get(key) is None:
                        continue
                    turn["tokens"].setdefault(key, None)
                    turn["tokens"][key] = (turn["tokens"][key] or 0) + value
                    if invocation is not None:
                        invocation["tokens"].setdefault(key, None)
                        invocation["tokens"][key] = (invocation["tokens"][key] or 0) + value
        if not result["model"] and payload.get("model"):
            result["model"] = payload["model"]
        previous_was_tool_result = False
    if records and isinstance(records[0], dict):
        first = records[0]
        result["id"] = str(first.get("sessionId") or first.get("session_id") or first.get("conversationId") or first.get("conversation_id") or result["id"])
        result["name"] = viewer.explicit_conversation_name(records, result["id"]) or str(first.get("title") or first.get("name") or first.get("summary") or first.get("conversationTitle") or result["id"])
        if result["name"] == result["id"]:
            result["name"] = _clean_title(
                viewer.derived_conversation_name(records, result["id"]), result["id"]
            )
        if result["name"] == result["id"]:
            result["name"] = _user_prompt_title(records, result["id"])
    result["turns"] = list(turns.values())
    result["model"] = viewer.model_from_records(records) or result["model"] or "claude"
    result["deployment"] = viewer.deployment_from_records(records)
    result["tokenFields"] = [
        key for key in viewer.CANONICAL_TOKEN_KEYS
        if key in session_token_fields
    ]
    for key in viewer.CANONICAL_TOKEN_KEYS:
        values = [turn["tokens"].get(key) for turn in result["turns"] if turn["tokens"].get(key) is not None]
        if values:
            result["tokens"][key] = sum(values)
    result["source"] = str(summary.get("_source", ""))
    children = summary.get("_children") if isinstance(summary.get("_children"), list) else []
    subagents = []
    subagent_tokens = viewer.blank_tokens()
    unlinked_subagent_tokens = viewer.blank_tokens()
    for child_summary in children:
        if not isinstance(child_summary, dict) or not isinstance(child_summary.get("_source"), Path):
            continue
        child = viewer.pricing.apply_costs(details(child_summary))
        child["relation"] = "subagent"
        metadata = child_summary.get("_agent_meta") if isinstance(child_summary.get("_agent_meta"), dict) else {}
        child["agentDescription"] = metadata.get("description") or child.get("name")
        child["agentModel"] = metadata.get("model") or child.get("model")
        child["spawnDepth"] = metadata.get("spawnDepth")
        child["toolUseId"] = metadata.get("toolUseId")
        subagents.append(child)
        reference = tool_calls.get(str(child["toolUseId"])) if child.get("toolUseId") else None
        if reference is not None:
            reference[2]["subagent"] = child
            # A turn's usage must represent all work initiated by that turn,
            # not only the parent Claude response.  Invocation cards already
            # roll delegated usage into their totals; mirror that aggregation
            # on the owning turn so the turn and session cards reconcile with
            # the invocation cards.
            owner_turn = reference[0]
            for key in viewer.TOKEN_KEYS:
                value = child.get("tokens", {}).get(key)
                if isinstance(value, int):
                    owner_turn["tokens"][key] = (owner_turn["tokens"][key] or 0) + value
        else:
            for key in viewer.TOKEN_KEYS:
                value = child.get("tokens", {}).get(key)
                if isinstance(value, int):
                    unlinked_subagent_tokens[key] = (unlinked_subagent_tokens[key] or 0) + value
        for key in viewer.TOKEN_KEYS:
            value = child.get("tokens", {}).get(key)
            if isinstance(value, int):
                subagent_tokens[key] = (subagent_tokens[key] or 0) + value
    result["ownTokens"] = dict(result["tokens"])
    result["subagents"] = subagents
    result["subagentTokens"] = subagent_tokens
    for key in viewer.TOKEN_KEYS:
        # Recompute after delegated usage has been attached to its owning
        # turn; the earlier session sum intentionally runs before children
        # are discovered.
        turn_values = [
            turn["tokens"][key]
            for turn in result["turns"]
            if turn["tokens"][key] is not None
        ]
        result["tokens"][key] = sum(turn_values) if turn_values else None
        if unlinked_subagent_tokens[key] is not None:
            result["tokens"][key] = (result["tokens"][key] or 0) + unlinked_subagent_tokens[key]
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
    return create_archive("anthropic_claude", archive, files)


def import_source_files(archive: Path, root: Path) -> list[Path]:
    destination = Path.home() / ".claude" / "projects" / "imported"
    return inject_archive("anthropic_claude", archive, destination)
