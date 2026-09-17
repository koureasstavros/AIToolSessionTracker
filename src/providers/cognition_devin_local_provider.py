"""Cognition Devin local SQLite session discovery and parsing."""
from __future__ import annotations

import json
import os
import re
import sqlite3
from contextlib import closing
from pathlib import Path

from src.common.source_archive import create_archive, inject_archive

TOKEN_FIELDS = {
    "inputTokens": ("input_tokens", "inputTokens"),
    "cacheReadTokens": ("cache_read_tokens", "cacheReadTokens"),
    "cacheWriteTokens": ("cache_creation_tokens", "cacheWriteTokens"),
    "outputTokens": ("output_tokens", "outputTokens", "num_tokens"),
    "reasoningTokens": ("reasoning_tokens", "reasoningTokens"),
}
UUID_PATTERN = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)


def _viewer():
    import session_token_viewer as viewer
    return viewer


def display_root(root: Path | None = None) -> Path:
    configured = os.environ.get("COGNITION_DEVIN_DB") or os.environ.get("DEVIN_DB")
    if configured:
        return Path(configured).expanduser()
    if root and root.is_file() and root.name.lower() == "sessions.db":
        return root
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "Devin" / "cli" / "sessions.db"
    return Path.home() / ".config" / "devin" / "cli" / "sessions.db"


def tool(summary: dict) -> str:
    return str(summary.get("_surface") or "CLI")


def identity(record: dict, fallback: str) -> tuple[str, str]:
    session_id = str(record.get("session_id") or record.get("sessionId") or fallback)
    return session_id, str(record.get("title") or session_id)


def _connect(root: Path | None = None) -> sqlite3.Connection | None:
    path = display_root(root)
    if not path.is_file():
        return None
    try:
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        return connection
    except (OSError, sqlite3.Error):
        return None


def _json(value: object) -> dict:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _sessions(root: Path | None = None) -> list[dict]:
    connection = _connect(root)
    if connection is None:
        return []
    try:
        with closing(connection):
            return [dict(row) for row in connection.execute(
                "SELECT id, working_directory, model, agent_mode, created_at, last_activity_at, title, hidden FROM sessions ORDER BY last_activity_at DESC"
            )]
    except sqlite3.Error:
        return []


def _message_rows(connection: sqlite3.Connection, session_id: str) -> list[dict]:
    rows = connection.execute(
        "SELECT node_id, chat_message, created_at FROM message_nodes WHERE session_id = ? ORDER BY node_id",
        (session_id,),
    ).fetchall()
    result = []
    seen_message_ids: set[str] = set()
    for node_id, raw, created_at in rows:
        message = _json(raw)
        if message:
            message_id = message.get("message_id")
            if message_id and str(message_id) in seen_message_ids:
                continue
            if message_id:
                seen_message_ids.add(str(message_id))
            message["_node_id"] = node_id
            message["_created_at"] = created_at
            result.append(message)
    return result


def _content(message: dict) -> str:
    value = message.get("content", "")
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return "\n".join(_content(part) if isinstance(part, dict) else str(part) for part in value).strip()
    if isinstance(value, dict):
        return str(value.get("text") or value.get("content") or "").strip()
    return str(value).strip() if value else ""


def _has_data(rows: list[dict]) -> bool:
    return any(row.get("role") in {"user", "assistant", "model"} and _content(row) for row in rows)


def _display_id(session_id: str, rows: list[dict]) -> str:
    if UUID_PATTERN.fullmatch(session_id):
        return session_id
    for row in rows:
        match = UUID_PATTERN.fullmatch(str(row.get("message_id") or ""))
        if match:
            return match.group(0)
    return session_id


def _model(session: dict, rows: list[dict]) -> str | None:
    value = str(session.get("model") or "").strip()
    if value:
        return value
    for row in rows:
        metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        model = metadata.get("generation_model") or metadata.get("model")
        if model:
            return str(model)
    return None


def _surface(session: dict, rows: list[dict], db_path: Path) -> str:
    text = "\n".join(_content(row).lower() for row in rows)
    if re.search(r"devin desktop|inside devin desktop|devin desktop", text):
        return "Desktop"
    if re.search(r"vscode|visual studio code|devin extension", text):
        return "Extension"
    if re.search(r"interactive command line agent|devin cli", text) or "\\cli\\" in str(db_path).lower():
        return "CLI"
    return "CLI"


def _title(session: dict, rows: list[dict]) -> str:
    title = str(session.get("title") or "").strip()
    if title:
        return title
    for row in rows:
        if row.get("role") == "user" and _content(row):
            text = " ".join(_content(row).split())
            return text[:77].rstrip() + "..." if len(text) > 80 else text
    return str(session.get("id") or "Devin session")


def index(root: Path | None = None) -> list[dict]:
    viewer = _viewer()
    db_path = display_root(root)
    entries = []
    connection = _connect(root)
    if connection is None:
        return entries
    try:
        with closing(connection):
            for session in _sessions(root):
                session_id = str(session["id"])
                rows = _message_rows(connection, session_id)
                display_id = _display_id(session_id, rows)
                surface = _surface(session, rows, db_path)
                entry = {
                    "_source": db_path,
                    "_provider": "cognition_devin",
                    "_kind": "external",
                    "id": display_id,
                    "name": _title(session, rows),
                    "updated": session.get("last_activity_at") or 0,
                    "project": session.get("working_directory") or None,
                    "model": _model(session, rows),
                    "_has_data": _has_data(rows),
                    "_source_label": surface,
                    "_surface": surface,
                    "_devin_session": session,
                    "_devin_session_id": session_id,
                }
                if session.get("hidden"):
                    entry["_has_data"] = False
                entries.append(entry)
    except sqlite3.Error:
        return []
    return sorted(entries, key=lambda item: item.get("updated", 0), reverse=True)


def _usage(metadata: dict) -> tuple[dict[str, int | None], bool]:
    metrics = metadata.get("metrics") if isinstance(metadata.get("metrics"), dict) else {}
    values = {key: None for key in _viewer().TOKEN_KEYS}
    found = False
    for output_key, candidates in TOKEN_FIELDS.items():
        for candidate in candidates:
            value = metrics.get(candidate, metadata.get(candidate))
            if isinstance(value, (int, float)) and value >= 0:
                values[output_key] = int(value)
                found = True
                break
    return values, found


def _estimate(text: str) -> int:
    return max(1, len(text) // 4) if text else 0


def details(summary: dict) -> dict:
    viewer = _viewer()
    db_path = Path(summary["_source"])
    session_meta = summary.get("_devin_session") if isinstance(summary.get("_devin_session"), dict) else {}
    session_id = str(summary.get("_id") or summary.get("id") or "")
    db_session_id = str(summary.get("_devin_session_id") or session_meta.get("id") or session_id)
    connection = _connect(db_path)
    updated = float(summary.get("updated") or 0)
    result = viewer.new_session(session_id, summary.get("name") or session_id, updated, model=summary.get("model") or _model(session_meta, []), project=session_meta.get("working_directory") or summary.get("project"))
    turns: list[dict] = []
    current: dict | None = None
    invocation: dict | None = None
    estimated = False
    if connection is None:
        result["turns"] = turns
        result["tokenFlags"] = ["estimated"]
        return result
    try:
        with closing(connection):
            rows = _message_rows(connection, db_session_id)
    except sqlite3.Error:
        rows = []
    surface = str(summary.get("_surface") or _surface(session_meta, rows, db_path))
    result["surface"] = surface
    if not result.get("model"):
        result["model"] = _model(session_meta, rows)

    for message in rows:
        role = message.get("role")
        text = _content(message)
        raw = json.dumps(message, indent=2, ensure_ascii=False)
        if role == "user" and text:
            current = viewer.new_turn(f"turn-{len(turns) + 1}")
            current["user"] = text
            current["raw"].append(raw)
            turns.append(current)
            invocation = None
        elif current is not None:
            current["raw"].append(raw)
            if role in {"assistant", "model"}:
                if invocation is None:
                    invocation = {"index": len(current.setdefault("invocations", [])) + 1, "tokens": viewer.blank_tokens(), "tools": [], "assistant": [], "thinking": []}
                    current["invocations"].append(invocation)
                if text:
                    current["assistant"].append(text)
                    invocation["assistant"].append(text)
                usage, has_usage = _usage(message.get("metadata") or {})
                if has_usage:
                    for key, value in usage.items():
                        if value is not None:
                            invocation["tokens"][key] = value
                else:
                    estimated = True
                    invocation["tokens"]["outputTokens"] = _estimate(text)
                    invocation["tokens"]["reasoningTokens"] = 0
            elif role in {"tool", "tool_result"} and text:
                current.setdefault("assistant", []).append(text)

    for turn in turns:
        if turn.get("invocations"):
            for invocation in turn["invocations"]:
                if invocation["tokens"]["inputTokens"] is None:
                    estimated = True
                    invocation["tokens"]["inputTokens"] = _estimate(turn.get("user", ""))
                for key in viewer.TOKEN_KEYS:
                    invocation["tokens"][key] = invocation["tokens"][key] if invocation["tokens"][key] is not None else 0
                invocation["tokenFields"] = list(viewer.TOKEN_KEYS)
                for key in viewer.TOKEN_KEYS:
                    turn["tokens"][key] = (turn["tokens"][key] or 0) + invocation["tokens"][key]
        else:
            estimated = True
            turn["tokens"].update(inputTokens=_estimate(turn.get("user", "")), cacheReadTokens=0, cacheWriteTokens=0, outputTokens=0, reasoningTokens=0)
        turn["tokenFields"] = list(viewer.TOKEN_KEYS)

    for key in viewer.TOKEN_KEYS:
        values = [turn["tokens"][key] for turn in turns if turn["tokens"][key] is not None]
        if values:
            result["tokens"][key] = sum(values)
    result["turns"] = turns
    result["source"] = str(db_path)
    result["ownTokens"] = dict(result["tokens"])
    result["tokenFields"] = list(viewer.TOKEN_KEYS)
    result["tokenFlags"] = ["estimated"] if estimated else []
    result["subagents"] = []
    result["subagentTokens"] = viewer.blank_tokens()
    return result


def delete(summary: dict) -> None:
    db_path = Path(summary.get("_source", ""))
    session_meta = summary.get("_devin_session") if isinstance(summary.get("_devin_session"), dict) else {}
    session_id = str(summary.get("_devin_session_id") or session_meta.get("id") or summary.get("id") or "")
    if not session_id:
        return
    connection = _connect(db_path)
    if connection is None:
        return
    try:
        with closing(connection), connection:
            for table in ("message_nodes", "prompt_history", "rendered_commits", "tool_call_state", "subagent_heads"):
                connection.execute(f"DELETE FROM {table} WHERE session_id = ?", (session_id,))
            connection.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
    except sqlite3.Error:
        return


def export_source_files(summary: dict, archive: Path) -> Path:
    return create_archive("cognition_devin", archive, [(Path(summary["_source"]), ".")])


def import_source_files(archive: Path, root: Path) -> list[Path]:
    destination = display_root(root).parent
    return inject_archive("cognition_devin", archive, destination)
