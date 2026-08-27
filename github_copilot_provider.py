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


def _read_legacy(folder: Path) -> dict:
    """Parse the legacy workspace.yaml/events.jsonl Copilot format."""
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
    links: dict[str, str] = {}
    output_total = 0

    for event in records:
        data = event.get("data") or {}
        if not isinstance(data, dict):
            continue
        event_type = event.get("type", "")
        turn_id = str(data.get("turnId", ""))
        interaction = str(data.get("interactionId") or links.get(turn_id, ""))
        if not interaction and event_type in {"assistant.turn_start", "user.message", "assistant.message"}:
            interaction = turn_id or f"turn-{len(turns) + 1}"
        if interaction:
            turn = turns.setdefault(interaction, viewer.new_turn(interaction))
            turn["raw"].append(json.dumps(event, indent=2, ensure_ascii=False))
        if event_type == "session.start":
            session["model"] = data.get("selectedModel")
        elif event_type == "assistant.turn_start":
            links[turn_id] = interaction or turn_id
            turns.setdefault(links[turn_id], viewer.new_turn(links[turn_id]))
        elif event_type == "user.message":
            turns.setdefault(interaction, viewer.new_turn(interaction))["user"] = str(data.get("content", ""))
        elif event_type == "assistant.message":
            turn = turns.setdefault(interaction, viewer.new_turn(interaction))
            if data.get("content"):
                turn["assistant"].append(str(data["content"]))
            viewer.add_token_usage(turn["tokens"], data)
            value = viewer.number(data.get("outputTokens"))
            if value is not None:
                output_total += value
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
            usage = db.execute("SELECT turn_index, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, reasoning_tokens FROM assistant_usage_events WHERE session_id = ? ORDER BY id", (session_id,)).fetchall()
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
        for step, tokens in enumerate(steps, 1):
            turn = viewer.new_turn(f"{index}.{step}" if len(steps) > 1 else str(index))
            turn["user"] = user or "" if step == 1 else ""
            turn["assistant"] = [assistant] if assistant and step == 1 else []
            turn["tokens"].update({key: value if isinstance(value, int) else 0 for key, value in tokens.items()})
            turn["turn_index"], turn["step"], turn["step_count"] = index, step if len(steps) > 1 else None, len(steps)
            turn["raw"].append(json.dumps({"user": user, "assistant": assistant, "step": step}, ensure_ascii=False))
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
    return "CLI" if summary.get("_kind") == "copilot-db" else "Extension"


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
                    entries.append(viewer.session_summary(path, "copilot", "copilot-legacy"))
        except OSError:
            continue
    entries.extend(_db_index())
    unique: dict[str, dict] = {}
    for entry in entries:
        existing = unique.get(entry["id"])
        if existing is None or existing.get("_kind") == "copilot-db":
            unique[entry["id"]] = entry
        elif entry.get("_kind") == "copilot-db":
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
                "WHERE id IN (SELECT DISTINCT session_id FROM turns) ORDER BY updated_at DESC"
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
             "_source": path, "_session_id": sid, "_source_label": "Copilot CLI", "_kind": "copilot-db"}
            for sid, name, updated in rows]


def details(summary: dict) -> dict:
    viewer = _viewer()
    kind = summary["_kind"]
    if kind == "copilot-db":
        result = _read_db(summary["_session_id"], summary["_source"])
    elif kind == "copilot-chat":
        result = _read_chat(summary["_source"])
    else:
        result = _read_legacy(summary["_source"])
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
        if summary["_kind"] == "copilot-legacy":
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
