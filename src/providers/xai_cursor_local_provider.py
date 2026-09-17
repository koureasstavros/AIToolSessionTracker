"""xAI Cursor agent transcript discovery and parsing.

xAI Cursor stores local agent conversations as JSONL files below
``~/.cursor/projects/<project>/agent-transcripts/<session>/<session>.jsonl``.
The export currently does not contain provider token usage, so this adapter
estimates tokens from visible text and serialized tool arguments/results.
"""
from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path

from src.common.source_archive import create_archive, inject_archive

USER_QUERY_PATTERN = re.compile(r"<user_query>\s*(.*?)\s*</user_query>", re.DOTALL | re.IGNORECASE)
TIMESTAMP_PATTERN = re.compile(r"<timestamp>.*?</timestamp>", re.DOTALL | re.IGNORECASE)


def _viewer():
    import session_token_viewer as viewer
    return viewer


def display_root(root: Path | None = None) -> Path:
    configured = os.environ.get("XAI_CURSOR_ROOT") or os.environ.get("CURSOR_ROOT")
    if configured:
        return Path(configured).expanduser()
    if root is not None and (".cursor" in str(root).lower() or "cursor" in str(root).lower()):
        return root
    return Path.home() / ".cursor" / "projects"


def tool(summary: dict) -> str:
    return str(summary.get("_surface") or "Desktop")


def _surface(records: list[dict], path: Path | None = None) -> str:
    text = "\n".join(json.dumps(record, ensure_ascii=False).lower() for record in records)
    surfaces = set()
    if re.search(r"cursor[ _-]*(?:cli|command[- ]line)|command[- ]line cursor", text):
        surfaces.add("CLI")
    if re.search(r"vscode|vs code|visual studio code|cursor[ _-]*(?:extension|plugin)", text):
        surfaces.add("Extension")
    if re.search(r"desktop cursor|cursor desktop", text):
        surfaces.add("Desktop")
    if len(surfaces) > 1:
        return "Mixed"
    if surfaces:
        return next(iter(surfaces))
    if path and "cli" in str(path).lower():
        return "CLI"
    return "Desktop"


def _model(records: list[dict]) -> str | None:
    model_keys = {"model", "model_id", "modelId", "deployment", "generation_model", "generationModel"}

    def visit(value: object) -> str | None:
        if isinstance(value, dict):
            for key in model_keys:
                candidate = value.get(key)
                if isinstance(candidate, str) and candidate.strip():
                    return candidate.strip()
            for child in value.values():
                found = visit(child)
                if found:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = visit(child)
                if found:
                    return found
        return None

    return visit(records)


def _message(record: dict) -> dict:
    value = record.get("message")
    return value if isinstance(value, dict) else record


def _role(record: dict) -> object:
    return record.get("role") or _message(record).get("role")


def _content_parts(message: dict) -> list[object]:
    content = message.get("content")
    if isinstance(content, list):
        return content
    return [] if content is None else [content]


def _text(parts: list[object]) -> str:
    values: list[str] = []
    for part in parts:
        if isinstance(part, dict):
            if part.get("type") in {"tool_call", "tool_use", "tool_result"}:
                continue
            value = part.get("text") or part.get("content")
        else:
            value = part
        if isinstance(value, str) and value.strip():
            values.append(value.strip())
    return "\n".join(values)


def _user_title(text: str, fallback: str) -> str:
    match = USER_QUERY_PATTERN.search(text)
    if match:
        text = match.group(1)
    text = TIMESTAMP_PATTERN.sub("", text).strip()
    return " ".join(text.split())[:80] or fallback


def _has_data(path: Path) -> bool:
    for record in _viewer().safe_json_lines(path):
        message = _message(record)
        role = _role(record)
        if role in {"user", "assistant", "model"} and _text(_content_parts(message)):
            return True
    return False


def _files(root: Path | None = None) -> list[Path]:
    candidates = [display_root(root)]
    if root is not None and candidates[0] != root:
        candidates.append(root)
    files: list[Path] = []
    for base in candidates:
        if not base.exists():
            continue
        if base.is_file() and base.suffix.lower() == ".jsonl":
            files.append(base)
            continue
        try:
            files.extend(base.glob("*/agent-transcripts/*/*.jsonl"))
        except OSError:
            continue
    return list(dict.fromkeys(files))


def identity(record: dict, fallback: str) -> tuple[str, str]:
    message = _message(record)
    session_id = str(record.get("sessionId") or record.get("session_id") or fallback)
    text = _text(_content_parts(message))
    return session_id, _user_title(text, str(record.get("title") or fallback))


def index(root: Path | None = None) -> list[dict]:
    viewer = _viewer()
    entries: list[dict] = []
    for path in _files(root):
        session_id = path.parent.name
        records = viewer.safe_json_lines(path)
        surface = _surface(records, path)
        entry = viewer.session_summary(path, "xai_cursor", "external")
        entry["id"] = session_id
        first_user = next(
            (_text(_content_parts(_message(record))) for record in records
             if _role(record) == "user" and _text(_content_parts(_message(record)))),
            "",
        )
        entry["name"] = _user_title(first_user, session_id)
        entry["model"] = _model(records)
        entry["_surface"] = surface
        entry["_has_data"] = _has_data(path)
        entry["_source_label"] = tool(entry)
        entries.append(entry)
    return sorted(entries, key=lambda item: item.get("updated", 0), reverse=True)


def _estimate(value: object) -> int:
    if not isinstance(value, str) or not value:
        return 0
    return max(1, len(value) // 4)


def details(summary: dict) -> dict:
    viewer = _viewer()
    path = summary["_source"]
    try:
        updated = path.stat().st_mtime
    except OSError:
        updated = 0
    session_id = str(summary.get("id") or path.parent.name)
    records = viewer.safe_json_lines(path)
    surface = str(summary.get("_surface") or _surface(records, path))
    result = viewer.new_session(session_id, summary.get("name") or session_id, updated, model=summary.get("model") or _model(records))
    result["surface"] = surface
    result["tokenFlags"] = ["estimated"]
    turns: list[dict] = []
    current: dict | None = None
    invocation: dict | None = None
    pending_tools: list[dict] = []

    for record in records:
        message = _message(record)
        role = _role(record)
        parts = _content_parts(message)
        text = _text(parts)
        raw = json.dumps(record, indent=2, ensure_ascii=False)
        if role == "user":
            current = viewer.new_turn(f"turn-{len(turns) + 1}")
            current["user"] = _user_title(text, text)
            current["raw"].append(raw)
            turns.append(current)
            invocation = None
            pending_tools = []
            continue
        if current is None:
            continue
        current["raw"].append(raw)
        if role in {"assistant", "model"}:
            if invocation is None:
                invocation = {"index": len(current.setdefault("invocations", [])) + 1, "tokens": viewer.blank_tokens(), "tools": [], "assistant": [], "thinking": []}
                current["invocations"].append(invocation)
            if text:
                current["assistant"].append(text)
                invocation["assistant"].append(text)
            for part in parts:
                if not isinstance(part, dict) or part.get("type") not in {"tool_call", "tool_use"}:
                    continue
                tool_item = {"id": str(part.get("id") or f"{current['id']}-tool-{len(current.get('tools', [])) + 1}"), "name": str(part.get("name") or part.get("tool") or "tool"), "arguments": part.get("arguments") or part.get("input") or {}, "status": "started"}
                current.setdefault("tools", []).append(tool_item)
                invocation["tools"].append(tool_item)
                pending_tools.append(tool_item)
            continue
        if role in {"tool", "tool_result"} or any(isinstance(part, dict) and part.get("type") == "tool_result" for part in parts):
            if pending_tools:
                tool_item = pending_tools.pop(0)
                tool_item["status"] = "completed"
                tool_item["result"] = text if text else json.dumps(parts, ensure_ascii=False)
            elif text:
                current.setdefault("assistant", []).append(text)

    for turn in turns:
        running_input = _estimate(turn.get("user", ""))
        for inv in turn.get("invocations", []):
            args = sum(_estimate(json.dumps(tool.get("arguments", {}), ensure_ascii=False)) for tool in inv.get("tools", []))
            results = sum(_estimate(str(tool.get("result", ""))) for tool in inv.get("tools", []))
            output = sum(_estimate(text) for text in inv.get("assistant", []))
            inv["tokens"].update(inputTokens=running_input + args, cacheReadTokens=0, cacheWriteTokens=0, outputTokens=output, reasoningTokens=0)
            inv["tokenFields"] = list(viewer.TOKEN_KEYS)
            running_input += args + results
            for key in viewer.TOKEN_KEYS:
                turn["tokens"][key] = (turn["tokens"][key] or 0) + (inv["tokens"][key] or 0)
        if not turn.get("invocations"):
            turn["tokens"].update(inputTokens=_estimate(turn.get("user", "")), cacheReadTokens=0, cacheWriteTokens=0, outputTokens=_estimate("\n".join(turn.get("assistant", []))), reasoningTokens=0)
        turn["tokenFields"] = list(viewer.TOKEN_KEYS)

    for key in viewer.TOKEN_KEYS:
        values = [turn["tokens"][key] for turn in turns if turn["tokens"][key] is not None]
        if values:
            result["tokens"][key] = sum(values)
    result["turns"] = turns
    result["source"] = str(path)
    result["ownTokens"] = dict(result["tokens"])
    result["tokenFields"] = list(viewer.TOKEN_KEYS)
    result["subagents"] = []
    result["subagentTokens"] = viewer.blank_tokens()
    return result


def delete(summary: dict) -> None:
    source = summary.get("_source")
    if isinstance(source, Path) and source.exists():
        session_dir = source.parent
        shutil.rmtree(session_dir, ignore_errors=True)


def export_source_files(summary: dict, archive: Path) -> Path:
    return create_archive("xai_cursor", archive, [(summary["_source"], ".")])


def import_source_files(archive: Path, root: Path) -> list[Path]:
    destination = display_root(root) / "imported"
    return inject_archive("xai_cursor", archive, destination)
