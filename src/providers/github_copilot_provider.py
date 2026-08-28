"""GitHub Copilot session sources and persistence operations."""
from __future__ import annotations

import sqlite3
import json
import os
from pathlib import Path


def _viewer():
    # Lazy import keeps the executable module's shared data model authoritative
    # and avoids an import cycle during startup.
    import session_token_viewer as viewer
    return viewer


def _read_session_state(folder: Path) -> dict:
    """Parse the Copilot session-state workspace.yaml/events.jsonl format."""
    viewer = _viewer()
    try:
        updated = folder.stat().st_mtime
    except OSError:
        updated = 0
    session = viewer.new_session(folder.name, folder.name, updated)
    workspace = folder / "workspace.yaml"
    try:
        for line in workspace.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("name:"):
                session["name"] = line.partition(":")[2].strip().strip('"\'') or folder.name
            elif line.startswith(("path:", "folder:", "workspace:")):
                session["project"] = viewer.project_path(line.partition(":")[2].strip().strip('"\''))
    except OSError:
        pass
    events = folder / "events.jsonl"
    if not events.exists():
        return session
    records = viewer.safe_json_lines(events)
    session["project"] = viewer.project_from_records(records)
    turns: dict[str, dict] = {}
    turn_interactions: dict[str, str] = {}
    output_total = 0

    def get_turn(key: str) -> dict:
        return turns.setdefault(key, viewer.new_turn(key))

    for event in records:
        data = event.get("data") or {}
        if not isinstance(data, dict):
            continue
        event_type = event.get("type", "")
        turn_id = str(data.get("turnId", "")).strip()
        interaction_id = str(data.get("interactionId") or turn_interactions.get(turn_id, "")).strip()
        if not interaction_id and event_type in {"assistant.turn_start", "user.message", "assistant.message"}:
            interaction_id = turn_id or f"turn-{len(turns) + 1}"
        if interaction_id:
            turn = get_turn(interaction_id)
            turn["raw"].append(json.dumps(event, indent=2, ensure_ascii=False))
        if event_type == "session.start":
            session["model"] = data.get("selectedModel")
        elif event_type == "assistant.turn_start":
            interaction_id = interaction_id or turn_id
            turn_interactions[turn_id] = interaction_id
            get_turn(interaction_id)
        elif event_type == "user.message":
            turn = get_turn(interaction_id)
            if not turn["user"]:
                turn["user"] = str(data.get("content", ""))
        elif event_type == "assistant.message":
            turn = get_turn(interaction_id)
            turn["_event_step_count"] = turn.get("_event_step_count", 0) + 1
            if data.get("content"):
                turn["assistant"].append(str(data["content"]))
            viewer.add_token_usage(turn["tokens"], data)
            value = viewer.number(data.get("outputTokens"))
            if value is not None:
                output_total += value
        elif event_type == "tool.execution_start":
            turn.setdefault("tools", []).append({
                "id": data.get("toolCallId"),
                "name": data.get("toolName") or "unknown",
                "arguments": data.get("arguments"),
                "status": "started",
            })
        elif event_type == "tool.execution_complete":
            tool_id = data.get("toolCallId")
            tools = turn.setdefault("tools", [])
            tool = next((item for item in tools if item.get("id") == tool_id), None)
            if tool is None:
                tool = {"id": tool_id, "name": data.get("toolName") or "unknown"}
                tools.append(tool)
            tool["status"] = "completed" if data.get("success", True) else "failed"
            if data.get("result"):
                tool["result"] = data["result"]
        elif event_type == "session.shutdown":
            metrics = data.get("modelMetrics") or {}
            if isinstance(metrics, dict):
                for model_name, model in metrics.items():
                    if isinstance(model, dict):
                        session["model"] = session["model"] or str(model_name)
                        viewer.add_token_usage(session["tokens"], model.get("usage") or {})

    session["turns"] = list(turns.values())
    if output_total:
        session["tokens"]["outputTokens"] = output_total
    if len(session["turns"]) == 1:
        for key in viewer.TOKEN_KEYS:
            if session["turns"][0]["tokens"][key] is None:
                session["turns"][0]["tokens"][key] = session["tokens"][key]
    elif session["turns"]:
        weights = [max(turn["tokens"]["outputTokens"] or 0, 1) for turn in session["turns"]]
        total_weight = sum(weights)
        for key in ("inputTokens", "cacheReadTokens", "cacheWriteTokens", "reasoningTokens"):
            total = session["tokens"][key]
            if total is None:
                continue
            assigned = 0
            for index, turn in enumerate(session["turns"]):
                value = total - assigned if index == len(weights) - 1 else int(total * weights[index] / total_weight)
                assigned += value
                turn["tokens"][key] = value
    return session


def _read_chat(path: Path) -> dict:
    """Parse the current VS Code chatSessions JSONL format."""
    viewer = _viewer()
    try:
        updated = path.stat().st_mtime
    except OSError:
        updated = 0
    session = viewer.new_session(path.stem, path.stem, updated)
    records = viewer.safe_json_lines(path)
    metadata = records[0].get("v", {}) if records else {}
    if not isinstance(metadata, dict):
        metadata = {}
    session["id"] = str(metadata.get("sessionId") or path.stem)
    session["name"] = str(metadata.get("customTitle") or session["id"])
    session["project"] = viewer.project_path(metadata.get("folder") or metadata.get("workspaceFolder") or metadata.get("projectPath"))
    if not session["project"]:
        session["project"] = viewer.workspace_project_path(path.parent.parent)
    requests = [dict(item) for item in metadata.get("requests", []) if isinstance(item, dict)]
    positions = {item.get("requestId"): index for index, item in enumerate(requests) if item.get("requestId")}
    for record in records:
        if record.get("k") == ["customTitle"]:
            session["name"] = str(record.get("v") or session["name"])
        key, value = record.get("k"), record.get("v")
        if key == ["requests"] and isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    request_id = item.get("requestId")
                    if request_id in positions:
                        requests[positions[request_id]].update(item)
                    else:
                        positions[request_id] = len(requests)
                        requests.append(dict(item))
        elif isinstance(key, list) and len(key) == 3 and key[0] == "requests" and isinstance(key[1], int):
            while len(requests) <= key[1]:
                requests.append({})
            requests[key[1]][str(key[2])] = value
    for index, request in enumerate(requests, 1):
        turn = viewer.new_turn(str(request.get("requestId") or index))
        message = request.get("message") or {}
        if isinstance(message, dict):
            turn["user"] = str(message.get("text") or "")
        response = request.get("response") or []
        if isinstance(response, list):
            turn["assistant"] = [str(item["value"]) for item in response if isinstance(item, dict) and item.get("value")]
        result = request.get("result") if isinstance(request.get("result"), dict) else {}
        metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
        turn["tokens"]["inputTokens"] = viewer.number(request.get("promptTokens") or metadata.get("promptTokens"))
        turn["tokens"]["outputTokens"] = viewer.number(
            request.get("completionTokens") or metadata.get("outputTokens") or metadata.get("completionTokens")
        )
        for usage_source in (request, metadata):
            for key, value in viewer.usage_from(usage_source).items():
                if value is not None:
                    turn["tokens"][key] = value
        if (not turn["assistant"]
            and turn["tokens"]["outputTokens"] is None
            and not turn["user"]
            and turn["tokens"]["inputTokens"] is None):
            continue
        turn["raw"].append(json.dumps(request, indent=2, ensure_ascii=False))
        session["turns"].append(turn)
        for key in ("inputTokens", "outputTokens"):
            session["tokens"][key] = (session["tokens"][key] or 0) + (turn["tokens"][key] or 0)
        tool_index = 0
        for response_item in response:
            if not isinstance(response_item, dict) or response_item.get("kind") != "toolInvocationSerialized":
                continue
            tool_index += 1
            tool_turn = viewer.new_turn(f"{request.get('requestId') or index}:tool:{tool_index}")
            tool_turn["kind"] = "tool"
            tool_turn["raw"].append(json.dumps(response_item, indent=2, ensure_ascii=False))
            tool_id = response_item.get("toolId") or response_item.get("toolCallId") or "unknown"
            invocation = response_item.get("invocationMessage") or response_item.get("pastTenseMessage") or ""
            if isinstance(invocation, dict):
                invocation = invocation.get("value") or ""
            tool_turn["assistant"].append(f"Tool: {tool_id}\n{invocation}".strip())
            details = response_item.get("resultDetails")
            if isinstance(details, dict):
                output = details.get("output")
                if output:
                    rendered = []
                    for item in output if isinstance(output, list) else [output]:
                        if isinstance(item, dict):
                            rendered.append(str(item.get("value") or item.get("text") or json.dumps(item, ensure_ascii=False)))
                        else:
                            rendered.append(str(item))
                    tool_turn["assistant"].append("Tool output:\n" + "\n".join(rendered))
            session["turns"].append(tool_turn)
    session["model"] = str(next((request.get("modelId") for request in requests if request.get("modelId")), "model unavailable"))
    return session


def _read_db(session_id: str, db_path: Path) -> dict:
    viewer = _viewer()
    session = viewer.new_session(session_id, session_id, db_path.stat().st_mtime, "GitHub Copilot")
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as db:
            columns = {row[1] for row in db.execute('PRAGMA table_info("sessions")')}
            project_column = next((name for name in ("cwd", "working_directory", "project_path") if name in columns), None)
            extra = f", {project_column}" if project_column else ""
            row = db.execute(f"SELECT summary, updated_at{extra} FROM sessions WHERE id = ?", (session_id,)).fetchone()
            turns = db.execute("SELECT turn_index, user_message, assistant_response FROM turns WHERE session_id = ? ORDER BY turn_index", (session_id,)).fetchall()
            usage = db.execute("SELECT turn_index, input_tokens, cache_read_tokens, cache_write_tokens, output_tokens, reasoning_tokens FROM assistant_usage_events WHERE session_id = ? ORDER BY id", (session_id,)).fetchall()
            if row:
                session["name"] = row[0] or session_id
                if project_column and row[2]:
                    candidate = viewer.project_path(row[2])
                    internal = (Path.home() / ".copilot" / "chats").resolve()
                    try:
                        session["project"] = None if candidate and Path(candidate).resolve().is_relative_to(internal) else candidate
                    except (OSError, ValueError):
                        session["project"] = candidate
    except (OSError, sqlite3.Error):
        return session
    by_turn: dict[int, list[dict]] = {}
    for index, *values in usage:
        by_turn.setdefault(index or 0, []).append(dict(zip(viewer.TOKEN_KEYS, values)))
    for index, user, assistant in turns:
        steps = by_turn.get(index) or [{key: 0 for key in viewer.TOKEN_KEYS}]
        # Several assistant_usage_events rows can belong to one logical turn.
        # Keep one UI turn card and retain the individual usage rows as steps
        # inside it instead of presenting every row as a separate conversation.
        turn = viewer.new_turn(str(index))
        turn["user"] = user or ""
        turn["assistant"] = [assistant] if assistant else []
        turn["turn_index"], turn["step_count"] = index, len(steps)
        turn["steps"] = []
        for step, tokens in enumerate(steps, 1):
            normalized_tokens = {
                key: value if isinstance(value, int) else 0
                for key, value in tokens.items()
            }
            turn["steps"].append({"index": step, "tokens": normalized_tokens})
            for key, value in normalized_tokens.items():
                turn["tokens"][key] = (turn["tokens"][key] or 0) + value
            turn["raw"].append(json.dumps({"user": user, "assistant": assistant, "step": step, "tokens": normalized_tokens}, ensure_ascii=False))
        if not turn["user"] and not turn["assistant"]:
            turn["kind"] = "usage_summary"
        session["turns"].append(turn)
    # Usage can be persisted even when the database has no corresponding row
    # in `turns` (for example, a session whose transcript is in session-state).
    # Keep those records as synthetic turns so they can supplement the content
    # parsed from the other source during multi-source merging.
    known_indices = {turn.get("turn_index") for turn in session["turns"]}
    for index, steps in by_turn.items():
        if index in known_indices:
            continue
        turn = viewer.new_turn(str(index))
        turn["kind"] = "usage_summary"
        turn["turn_index"], turn["step_count"] = index, len(steps)
        turn["steps"] = []
        for step, tokens in enumerate(steps, 1):
            normalized_tokens = {
                key: value if isinstance(value, int) else 0
                for key, value in tokens.items()
            }
            turn["steps"].append({"index": step, "kind": "usage_summary", "tokens": normalized_tokens})
            for key, value in normalized_tokens.items():
                turn["tokens"][key] = (turn["tokens"][key] or 0) + value
            turn["raw"].append(json.dumps({"source": "assistant_usage_events", "turn_index": index, "step": step, "tokens": normalized_tokens}, ensure_ascii=False))
        session["turns"].append(turn)
    for key in viewer.TOKEN_KEYS:
        values = [turn["tokens"][key] for turn in session["turns"]]
        session["tokens"][key] = sum(values) if any(values) else None
    return session


def default_root() -> Path:
    app_data = os.environ.get("APPDATA")
    if app_data:
        return Path(app_data) / "Code" / "User" / "workspaceStorage"
    return Path.home() / ".config" / "Code" / "User" / "workspaceStorage"


def display_root(root: Path) -> Path:
    return root


def tool(summary: dict) -> str:
    # The local Copilot database path identifies the storage family, but its
    # records do not contain a reliable CLI-versus-Desktop marker.
    return "CLI / Desktop" if summary.get("_kind") in {"copilot-db", "copilot-session-state"} else "Extension"


def identity(record: dict, fallback: str) -> tuple[str, str]:
    session_id = str(record.get("sessionId") or record.get("session_id") or fallback)
    return session_id, str(record.get("title") or record.get("name") or record.get("summary") or session_id)


def _roots(root: Path) -> list[Path]:
    roots = [root, Path.home() / ".copilot" / "session-state", default_root()]
    app_data = os.environ.get("APPDATA")
    global_root = (Path(app_data) / "Code" / "User" / "globalStorage" / "emptyWindowChatSessions"
                   if app_data else Path.home() / ".config" / "Code" / "User" / "globalStorage" / "emptyWindowChatSessions")
    roots.append(global_root)
    return list(dict.fromkeys(path for path in roots if path.is_dir()))


def _session_store() -> Path:
    return Path.home() / ".copilot" / "session-store.db"


def index(root: Path) -> list[dict]:
    viewer = _viewer()
    entries: list[dict] = []
    for scan_root in _roots(root):
        try:
            chat_files = (scan_root.glob("*.jsonl") if scan_root.name == "emptyWindowChatSessions"
                          else scan_root.rglob("chatSessions/*.jsonl"))
            entries.extend(viewer.session_summary(path, "copilot", "copilot-chat") for path in chat_files)
            candidates = [scan_root] if (scan_root / "events.jsonl").is_file() else list(scan_root.iterdir())
            for path in candidates:
                if path.is_dir() and path.name != "__pycache__" and (path / "events.jsonl").exists():
                    entries.append(viewer.session_summary(path, "copilot", "copilot-session-state"))
        except OSError:
            continue
    entries.extend(_db_index())
    unique: dict[str, dict] = {}
    for entry in entries:
        existing = unique.get(entry["id"])
        if existing is None:
            entry["_sources"] = [entry.copy()]
            unique[entry["id"]] = entry
            continue
        # A Copilot conversation can be represented by both session-state
        # events and the local database. Keep every representation so details
        # can combine the richer parts instead of discarding one source.
        existing.setdefault("_sources", [existing.copy()]).append(entry.copy())
        if existing.get("_kind") == "copilot-db" and entry.get("_kind") != "copilot-db":
            primary = entry
            primary["_sources"] = existing["_sources"]
            unique[entry["id"]] = primary
            existing = primary
        if entry.get("_kind") == "copilot-db":
            existing["_db_metadata"] = {key: entry.get(key) for key in ("name", "updated", "model")}
        if existing.get("name") in {None, "", existing["id"]} and entry.get("name"):
            existing["name"] = entry["name"]
        existing["updated"] = max(existing["updated"], entry["updated"])
    return sorted(unique.values(), key=lambda item: item["updated"], reverse=True)


def _db_index() -> list[dict]:
    viewer = _viewer()
    path = _session_store()
    if not path.exists():
        return []
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
            rows = db.execute(
                "SELECT id, COALESCE(summary, id), updated_at FROM sessions "
                "ORDER BY updated_at DESC"
            ).fetchall()
    except (OSError, sqlite3.Error):
        return []
    fallback = path.stat().st_mtime
    from datetime import datetime
    def timestamp(value: str | None) -> float:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() if value else fallback
        except (AttributeError, ValueError):
            return fallback
    return [{"id": sid, "name": name, "updated": timestamp(updated),
             "turns": [], "tokens": viewer.blank_tokens(), "model": "GitHub Copilot",
             "_source": path, "_session_id": sid, "_source_label": "CLI / Desktop", "_kind": "copilot-db"}
            for sid, name, updated in rows]


def details(summary: dict) -> dict:
    viewer = _viewer()
    sources = summary.get("_sources") or [summary]
    parsed: list[dict] = []
    for source in sources:
        kind = source["_kind"]
        if kind == "copilot-db":
            parsed.append(_read_db(source["_session_id"], source["_source"]))
        elif kind == "copilot-chat":
            parsed.append(_read_chat(source["_source"]))
        else:
            parsed.append(_read_session_state(source["_source"]))
    result = parsed[0]
    # The same session may have a sparse session-state event stream and a
    # complete database transcript. Fill missing messages and usage from every
    # source, matching turns by their stable id and then by ordinal position.
    for source_summary, supplement in zip(sources[1:], parsed[1:]):
        database_usage = source_summary.get("_kind") == "copilot-db"
        result["name"] = result.get("name") if result.get("name") not in {None, "", result["id"]} else supplement.get("name", result["name"])
        result["model"] = result.get("model") if result.get("model") not in {None, "", "model unavailable"} else supplement.get("model")
        result["project"] = result.get("project") or supplement.get("project")
        result["updated"] = max(result.get("updated", 0), supplement.get("updated", 0))
        if database_usage:
            # Database turn_index values can change after internal messages
            # such as injected skill context even though events.jsonl keeps
            # those API calls in one user interaction. The event stream owns
            # interaction boundaries; assistant-message counts identify which
            # ordered database usage rows belong to each interaction.
            database_steps = [
                step
                for database_turn in supplement.get("turns", [])
                for step in (database_turn.get("steps") or [])
                if isinstance(step, dict)
            ]
            event_step_counts = [
                turn.get("_event_step_count", 0)
                for turn in result["turns"]
            ]
            if database_steps and sum(event_step_counts) == len(database_steps):
                issue = (
                    "Copilot session-store.db usage rows were grouped using "
                    "the interaction boundaries from events.jsonl."
                )
                result["_db_issue"] = issue
                viewer.LOGGER.warning("%s Session %s", issue, result["id"])
                offset = 0
                for index, (turn, step_count) in enumerate(zip(result["turns"], event_step_counts)):
                    steps = [dict(step) for step in database_steps[offset:offset + step_count]]
                    offset += step_count
                    for step_index, step in enumerate(steps, 1):
                        step["index"] = step_index
                        step.pop("kind", None)
                    turn["steps"] = steps
                    turn["step_count"] = step_count
                    turn["turn_index"] = index
                    turn["tokens"] = viewer.blank_tokens()
                    for step in steps:
                        for key, value in step.get("tokens", {}).items():
                            turn["tokens"][key] = (turn["tokens"][key] or 0) + value
                for key in viewer.TOKEN_KEYS:
                    if supplement.get("tokens", {}).get(key) is not None:
                        result["tokens"][key] = supplement["tokens"][key]
                continue
        by_id = {str(turn.get("id")): turn for turn in result["turns"]}
        for index, other in enumerate(supplement.get("turns", [])):
            turn = by_id.get(str(other.get("id")))
            if turn is None and index < len(result["turns"]):
                turn = result["turns"][index]
            if turn is None:
                result["turns"].append(other)
                continue
            turn["user"] = turn.get("user") or other.get("user", "")
            if not turn.get("assistant"):
                turn["assistant"] = other.get("assistant", [])
            if not turn.get("raw"):
                turn["raw"] = other.get("raw", [])
            # A session-state transcript can provide the same logical turn
            # without the database usage-step metadata. Preserve that
            # metadata when the database representation is merged into it;
            # otherwise the first request is rendered as an unnumbered turn
            # while only the later request appears as "Step 2 of 2".
            for key in ("turn_index", "step", "step_count"):
                if other.get(key) is not None:
                    turn[key] = other[key]
            if database_usage and isinstance(other.get("steps"), list):
                turn["steps"] = [dict(step) for step in other["steps"] if isinstance(step, dict)]
                if turn.get("user") or turn.get("assistant"):
                    for step in turn["steps"]:
                        step.pop("kind", None)
            for key in viewer.TOKEN_KEYS:
                if database_usage and other.get("tokens", {}).get(key) is not None:
                    turn["tokens"][key] = other["tokens"][key]
                elif turn["tokens"].get(key) is None:
                    turn["tokens"][key] = other.get("tokens", {}).get(key)
        for key in viewer.TOKEN_KEYS:
            if database_usage and supplement.get("tokens", {}).get(key) is not None:
                result["tokens"][key] = supplement["tokens"][key]
            elif result["tokens"].get(key) is None:
                result["tokens"][key] = supplement.get("tokens", {}).get(key)
    viewer.subtract_cached_input(result)
    metadata = summary.get("_db_metadata")
    if isinstance(metadata, dict):
        if not result.get("name") or result["name"] == result["id"]:
            result["name"] = metadata.get("name") or result["name"]
        if not result.get("model") or result["model"] == "model unavailable":
            result["model"] = metadata.get("model") or result.get("model")
        result["updated"] = max(result.get("updated", 0), metadata.get("updated") or 0)
    if not result.get("project"):
        result["project"] = summary.get("project")
    result["source"] = str(summary.get("_source", ""))
    return result


def delete(summary: dict) -> None:
    viewer = _viewer()
    if summary["_kind"] != "copilot-db":
        source = summary["_source"]
        if summary["_kind"] == "copilot-session-state":
            import shutil
            shutil.rmtree(source)
        else:
            source.unlink()
        return
    session_id, db_path = summary["_session_id"], summary["_source"]
    with sqlite3.connect(db_path) as db:
        db.execute("PRAGMA foreign_keys = ON")
        tables = db.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        for (table,) in tables:
            if table == "sessions" or not table.replace("_", "").isalnum():
                continue
            columns = {row[1] for row in db.execute(f'PRAGMA table_info("{table}")')}
            if "session_id" in columns:
                db.execute(f'DELETE FROM "{table}" WHERE session_id = ?', (session_id,))
        db.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
