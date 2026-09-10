"""Google Antigravity transcript discovery and parsing.

Supports sessions created by:
- Antigravity IDE (~/.gemini/antigravity-ide/brain)
- Antigravity CLI (~/.gemini/antigravity-cli)
- Antigravity Desktop (~/.gemini/antigravity/brain)
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
from pathlib import Path

from src.common.source_archive import create_archive, inject_archive

UUID_PATTERN = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)
USER_REQUEST_PATTERN = re.compile(r"<USER_REQUEST>\s*(.*?)\s*(?:</USER_REQUEST>|\Z)", re.DOTALL | re.IGNORECASE)
MODEL_SETTING_PATTERN = re.compile(r"The user changed setting `Model Selection` from .*? to (.*?)\.", re.IGNORECASE)
WORKSPACE_URI_PATTERN = re.compile(r"file:///[a-zA-Z]:/[^\s\x00-\x1f\"'<>]+|[a-zA-Z]:\\[^\s\x00-\x1f\r\n\"'<>]+", re.IGNORECASE)


def _viewer():
    import session_token_viewer as viewer
    return viewer


def display_root(root: Path | None = None) -> Path:
    configured = os.environ.get("ANTIGRAVITY_ROOT")
    if configured:
        return Path(configured).expanduser()
    if root and root.exists() and root.is_dir() and ("gemini" in str(root).lower() or "antigravity" in str(root).lower()):
        return root
    ide_root = Path.home() / ".gemini" / "antigravity-ide" / "brain"
    if ide_root.exists():
        return ide_root
    return Path.home() / ".gemini" / "antigravity" / "brain"


def tool(summary: dict) -> str:
    source_str = str(summary.get("_source", "")).replace("\\", "/")
    if "antigravity-ide" in source_str:
        return "IDE"
    if "antigravity-cli" in source_str:
        return "CLI"
    if "/antigravity/" in source_str or source_str.endswith("/antigravity"):
        return "Desktop"
    return "IDE"


def identity(record: dict, fallback: str) -> tuple[str, str]:
    raw_id = str(record.get("sessionId") or record.get("session_id") or record.get("id") or fallback)
    match = UUID_PATTERN.search(raw_id)
    session_id = match.group(0) if match else raw_id
    name = record.get("name") or record.get("title") or record.get("summary")
    if name:
        return session_id, str(name)
    content = str(record.get("content") or "")
    m = USER_REQUEST_PATTERN.search(content)
    if m:
        req = " ".join(m.group(1).split()).strip()
        if req:
            short = req[:77].rstrip() + "..." if len(req) > 80 else req
            return session_id, short
    return session_id, fallback


def _has_data(path: Path) -> bool:
    viewer = _viewer()
    for record in viewer.safe_json_lines(path):
        stype = record.get("type")
        if stype in {"USER_INPUT", "PLANNER_RESPONSE"} and (record.get("content") or record.get("tool_calls")):
            return True
        if record.get("role") in {"user", "assistant", "model"} and record.get("content"):
            return True
    return False


def _candidate_roots(root: Path | None = None) -> list[Path]:
    candidates: list[Path] = []
    configured = os.environ.get("ANTIGRAVITY_ROOT")
    if configured:
        candidates.append(Path(configured).expanduser())
    if root and root.exists() and ("gemini" in str(root).lower() or "antigravity" in str(root).lower() or (root / "brain").exists()):
        candidates.append(root)
    candidates.append(Path.home() / ".gemini" / "antigravity-ide" / "brain")
    candidates.append(Path.home() / ".gemini" / "antigravity" / "brain")
    candidates.append(Path.home() / ".gemini" / "antigravity-cli")
    return list(dict.fromkeys(candidates))


def _files(root: Path | None = None) -> list[Path]:
    files: list[Path] = []
    for candidate in _candidate_roots(root):
        if not candidate.exists():
            continue
        try:
            if candidate.is_file() and candidate.suffix.lower() in {".json", ".jsonl"}:
                files.append(candidate)
                continue
            # Look for standard brain/<id>/.system_generated/logs/transcript.jsonl
            files.extend(candidate.glob("*/.system_generated/logs/transcript.jsonl"))
            # The CLI stores its session transcripts below antigravity-cli.
            # Restrict discovery to transcript files: chunk files such as
            # .system_generated/logs/chunks/transcript/00000000.jsonl are
            # parts of the same session, not separate sessions.
            if candidate.name == "antigravity-cli":
                files.extend(candidate.glob("**/transcript.jsonl"))
            # Also support imported or flat session transcripts
            files.extend(candidate.glob("imported/**/*.jsonl"))
            files.extend(candidate.glob("imported/**/*.json"))
        except OSError:
            continue
    return list(dict.fromkeys(files))


def _extract_title_from_brain_dir(conv_dir: Path) -> str | None:
    # Check task.md or implementation_plan.md
    for filename in ("task.md", "implementation_plan.md"):
        doc_path = conv_dir / filename
        if doc_path.is_file():
            try:
                for line in doc_path.read_text(encoding="utf-8", errors="replace").splitlines()[:5]:
                    line = line.strip()
                    if line.startswith("# "):
                        title = line.lstrip("# ").strip()
                        if title and not title.lower().startswith("task") and not title.lower().startswith("implementation"):
                            return title[:80]
            except OSError:
                pass
    return None


def _extract_title_from_transcript(path: Path, session_id: str) -> str:
    viewer = _viewer()
    for record in viewer.safe_json_lines(path):
        if record.get("type") == "USER_INPUT":
            content = str(record.get("content") or "")
            m = USER_REQUEST_PATTERN.search(content)
            if m:
                req = " ".join(m.group(1).split()).strip()
                if req:
                    return req[:77].rstrip() + "..." if len(req) > 80 else req
            cleaned = viewer.clean_session_prompt(content)
            if cleaned:
                return cleaned[:77].rstrip() + "..." if len(cleaned) > 80 else cleaned
    return session_id


def index(root: Path | None = None) -> list[dict]:
    viewer = _viewer()
    files = _files(root)
    entries: list[dict] = []
    seen_ids: set[str] = set()

    for path in files:
        if viewer.is_subagent_path(path):
            continue

        # Determine conversation ID
        if path.parent.name == "logs" and path.parent.parent.name == ".system_generated":
            conv_dir = path.parent.parent.parent
            conv_id = conv_dir.name
        else:
            conv_dir = path.parent
            conv_id = path.stem

        if conv_id in seen_ids:
            continue
        seen_ids.add(conv_id)

        entry = viewer.session_summary(path, "antigravity", "external")
        entry["id"] = conv_id

        # Determine display name
        title = _extract_title_from_brain_dir(conv_dir)
        if not title:
            title = _extract_title_from_transcript(path, conv_id)
        entry["name"] = title or conv_id

        entry["_has_data"] = _has_data(path)
        entry["_source_label"] = tool(entry)
        entries.append(entry)

    return sorted(entries, key=lambda item: item.get("updated", 0), reverse=True)


def _detect_model_and_project(conv_id: str, conv_dir: Path, records: list[dict]) -> tuple[str | None, str | None]:
    viewer = _viewer()
    model: str | None = None
    project: str | None = None

    # Check companion SQLite database in conversations/<id>.db
    # ~/.gemini/antigravity-ide/conversations/<id>.db
    potential_dbs = [
        conv_dir.parent.parent / "conversations" / f"{conv_id}.db",
        Path.home() / ".gemini" / "antigravity-ide" / "conversations" / f"{conv_id}.db",
        Path.home() / ".gemini" / "antigravity" / "conversations" / f"{conv_id}.db",
    ]
    for db_path in potential_dbs:
        if db_path.is_file():
            try:
                conn = sqlite3.connect(db_path)
                cur = conn.cursor()
                # 1. Project from trajectory_metadata_blob
                try:
                    cur.execute("SELECT data FROM trajectory_metadata_blob WHERE id = 'main'")
                    row = cur.fetchone()
                    if row and isinstance(row[0], bytes):
                        matches = WORKSPACE_URI_PATTERN.findall(row[0].decode("utf-8", errors="replace"))
                        for match in matches:
                            norm = viewer.project_path(match)
                            if norm:
                                project = norm
                                break
                except sqlite3.Error:
                    pass

                # 2. Model from gen_metadata
                try:
                    cur.execute("SELECT data FROM gen_metadata ORDER BY idx DESC LIMIT 5")
                    for row in cur.fetchall():
                        if row and isinstance(row[0], bytes):
                            text = row[0].decode("latin1", errors="replace")
                            for candidate in ("gemini-3.8-flash", "gemini-3.8-pro", "gemini-2.5-pro", "gemini-2.5-flash", "gemini-default", "Gemini 3.8 Flash (High)"):
                                if candidate in text:
                                    model = candidate
                                    break
                        if model:
                            break
                except sqlite3.Error:
                    pass
                conn.close()
            except sqlite3.Error:
                pass
        if model and project:
            break

    # If project not found in SQLite, inspect transcript content
    if not project:
        for record in records:
            content = str(record.get("content") or "")
            if "Active Document:" in content or "CorpusName" in content or "file:///" in content:
                matches = WORKSPACE_URI_PATTERN.findall(content)
                for match in matches:
                    norm = viewer.project_path(match)
                    if norm:
                        project = norm
                        break
            if project:
                break

    # If model not found, inspect transcript for settings changes or records
    if not model:
        for record in records:
            content = str(record.get("content") or "")
            m = MODEL_SETTING_PATTERN.search(content)
            if m:
                model = m.group(1).strip()
                break

    return model or "gemini-3.8-flash", project


def details(summary: dict) -> dict:
    viewer = _viewer()
    path = summary["_source"]
    try:
        updated = path.stat().st_mtime
    except OSError:
        updated = 0

    if path.parent.name == "logs" and path.parent.parent.name == ".system_generated":
        conv_dir = path.parent.parent.parent
        conv_id = conv_dir.name
    else:
        conv_dir = path.parent
        conv_id = summary.get("id") or path.stem

    records = viewer.safe_json_lines(path)
    model, project = _detect_model_and_project(conv_id, conv_dir, records)

    result = viewer.new_session(conv_id, summary.get("name") or conv_id, updated, model=model, project=project)

    turns: list[dict] = []
    current_turn: dict | None = None
    current_invocation: dict | None = None
    active_tools: list[dict] = []
    tools_executed = False

    def start_turn(turn_id: str) -> dict:
        nonlocal current_turn, current_invocation, active_tools, tools_executed
        current_turn = viewer.new_turn(turn_id)
        current_invocation = None
        active_tools = []
        tools_executed = False
        turns.append(current_turn)
        return current_turn

    def get_invocation(turn: dict) -> dict:
        nonlocal current_invocation
        if current_invocation is None:
            current_invocation = {
                "index": len(turn.setdefault("invocations", [])) + 1,
                "tokens": viewer.blank_tokens(),
                "tools": [],
                "assistant": [],
                "thinking": [],
                "isSubagent": False,
            }
            turn["invocations"].append(current_invocation)
        return current_invocation

    for record in records:
        step_type = record.get("type")
        content = record.get("content")
        raw_json = json.dumps(record, indent=2, ensure_ascii=False)

        if step_type == "USER_INPUT":
            turn = start_turn(f"turn-{len(turns) + 1}")
            turn["raw"].append(raw_json)
            text_content = str(content or "")

            # Extract user prompt from <USER_REQUEST>
            m = USER_REQUEST_PATTERN.search(text_content)
            user_text = m.group(1).strip() if m else text_content
            # Remove environment metadata tags from display
            user_text = re.sub(r"<(?:ADDITIONAL_METADATA|USER_SETTINGS_CHANGE)>.*?</(?:ADDITIONAL_METADATA|USER_SETTINGS_CHANGE)>", "", user_text, flags=re.DOTALL | re.IGNORECASE).strip()
            turn["user"] = user_text

            # Extract mentioned files
            mentioned = list(viewer.mentioned_files(text_content))
            # Also extract files from Active Document metadata
            for match in re.findall(r"Active Document:\s*([^\r\n]+)", text_content):
                doc = match.strip().partition("(")[0].strip()
                if doc and not any(f.get("path") == doc for f in mentioned if isinstance(f, dict)):
                    item = viewer.attached_file(Path(doc).name, doc)
                    if item:
                        mentioned.append(item)
            viewer.add_attached_files(turn, mentioned)
            continue

        if current_turn is None:
            # System message or initial setup before first user input
            continue

        current_turn["raw"].append(raw_json)

        if step_type == "PLANNER_RESPONSE":
            if tools_executed:
                current_invocation = None
                tools_executed = False
            invocation = get_invocation(current_turn)
            assistant_text = str(content or "").strip()
            if assistant_text:
                current_turn["assistant"].append(assistant_text)
                invocation["assistant"].append(assistant_text)

            thinking = record.get("thinking")
            if thinking and isinstance(thinking, str) and thinking.strip():
                invocation.setdefault("thinking", []).append(thinking.strip())

            # Parse tool calls initiated by planner response
            tool_calls = record.get("tool_calls")
            if isinstance(tool_calls, list):
                for tc in tool_calls:
                    if not isinstance(tc, dict):
                        continue
                    tool_name = tc.get("name", "unknown")
                    tool_args = tc.get("args") or tc.get("arguments") or {}
                    tool_item = {
                        "id": f"{current_turn['id']}-tool-{len(current_turn.get('tools', [])) + 1}",
                        "name": tool_name,
                        "arguments": tool_args,
                        "status": "started",
                    }
                    current_turn.setdefault("tools", []).append(tool_item)
                    invocation["tools"].append(tool_item)
                    active_tools.append(tool_item)

                    if viewer.is_subagent_invocation({"tools": [tool_item]}):
                        invocation["isSubagent"] = True

        elif step_type in {
            "RUN_COMMAND", "VIEW_FILE", "LIST_DIRECTORY", "GENERIC",
            "BROWSER_SUBAGENT", "CODE_ACTION", "ERROR_MESSAGE", "SYSTEM_MESSAGE"
        } or record.get("source") == "MODEL":
            # Tool result or execution step
            tools_executed = True
            status_val = record.get("status")
            is_error = status_val == "ERROR" or step_type == "ERROR_MESSAGE"
            tool_status = "error" if is_error else "completed"
            tool_result = str(content or "")

            if active_tools:
                tool_item = active_tools.pop(0)
                tool_item["status"] = tool_status
                tool_item["result"] = tool_result
            else:
                # Standalone tool event without prior tool_call
                invocation = get_invocation(current_turn)
                tool_item = {
                    "id": f"{current_turn['id']}-tool-{len(current_turn.get('tools', [])) + 1}",
                    "name": step_type.lower(),
                    "arguments": {},
                    "status": tool_status,
                    "result": tool_result,
                }
                current_turn.setdefault("tools", []).append(tool_item)
                invocation["tools"].append(tool_item)

            if step_type == "BROWSER_SUBAGENT" and current_invocation is not None:
                current_invocation["isSubagent"] = True

    # If no turns were captured, check if legacy role records exist
    if not turns:
        legacy_turn = viewer.new_turn("turn-1")
        for record in records:
            legacy_turn["raw"].append(json.dumps(record, indent=2, ensure_ascii=False))
            role = record.get("role") or record.get("source")
            cnt = record.get("content") or record.get("message") or ""
            if role == "user" and not legacy_turn["user"]:
                legacy_turn["user"] = str(cnt)
            elif role in {"assistant", "model"}:
                legacy_turn["assistant"].append(str(cnt))
        if legacy_turn["user"] or legacy_turn["assistant"]:
            turns.append(legacy_turn)

    def _estimate_tokens(val: object) -> int:
        if not isinstance(val, str) or not val:
            return 0
        return max(1, len(val) // 4)

    for turn in turns:
        user_tokens = _estimate_tokens(turn.get("user", ""))
        for f in turn.get("files", []):
            if isinstance(f, dict):
                user_tokens += _estimate_tokens(f.get("content", ""))

        running_input = user_tokens
        for inv in turn.get("invocations", []):
            ast_text = "\n".join(inv.get("assistant", []))
            thk_text = "\n".join(inv.get("thinking", []))

            raw_ast_tokens = _estimate_tokens(ast_text)
            reasoning_tokens = _estimate_tokens(thk_text)
            output_tokens = raw_ast_tokens + reasoning_tokens

            tool_args_tokens = sum(
                _estimate_tokens(json.dumps(t.get("arguments", {})))
                for t in inv.get("tools", [])
            )
            tool_res_tokens = sum(
                _estimate_tokens(str(t.get("result", "")))
                for t in inv.get("tools", [])
            )

            inv_input = running_input + tool_args_tokens
            running_input += tool_res_tokens + tool_args_tokens

            inv["tokens"]["inputTokens"] = inv_input
            inv["tokens"]["cacheReadTokens"] = 0
            inv["tokens"]["cacheWriteTokens"] = 0
            inv["tokens"]["outputTokens"] = output_tokens
            inv["tokens"]["reasoningTokens"] = reasoning_tokens

            for k in viewer.TOKEN_KEYS:
                turn["tokens"][k] = (turn["tokens"][k] or 0) + (inv["tokens"][k] or 0)

        if not turn.get("invocations") and (turn.get("user") or turn.get("assistant")):
            ast_text = "\n".join(turn.get("assistant", []))
            turn["tokens"]["inputTokens"] = user_tokens
            turn["tokens"]["cacheReadTokens"] = 0
            turn["tokens"]["cacheWriteTokens"] = 0
            turn["tokens"]["outputTokens"] = _estimate_tokens(ast_text)
            turn["tokens"]["reasoningTokens"] = 0

    for key in viewer.TOKEN_KEYS:
        vals = [turn["tokens"][key] for turn in turns if turn["tokens"][key] is not None]
        if vals:
            result["tokens"][key] = sum(vals)

    result["turns"] = turns
    result["source"] = str(summary.get("_source", ""))
    result["ownTokens"] = dict(result["tokens"])
    result["subagents"] = []
    result["subagentTokens"] = viewer.blank_tokens()

    return result


def delete(summary: dict) -> None:
    source = summary.get("_source")
    if not isinstance(source, Path) or not source.exists():
        return

    conv_id = summary.get("id")
    # If source is in brain/<id>/.system_generated/logs/transcript.jsonl
    if source.parent.name == "logs" and source.parent.parent.name == ".system_generated":
        conv_dir = source.parent.parent.parent
        if conv_dir.exists() and conv_dir.is_dir():
            shutil.rmtree(conv_dir, ignore_errors=True)

        # Also remove companion sqlite db and shm/wal/pb files
        potential_conv_dirs = [
            conv_dir.parent.parent / "conversations",
            Path.home() / ".gemini" / "antigravity-ide" / "conversations",
            Path.home() / ".gemini" / "antigravity" / "conversations",
        ]
        if conv_id:
            for cdir in potential_conv_dirs:
                if cdir.exists():
                    for match in cdir.glob(f"{conv_id}*"):
                        try:
                            match.unlink(missing_ok=True)
                        except OSError:
                            pass
    else:
        # Standalone imported or isolated transcript file
        source.unlink(missing_ok=True)


def export_source_files(summary: dict, archive: Path) -> Path:
    source = summary["_source"]
    files: list[tuple[Path, str]] = [(source, ".")]

    if source.name in {"transcript.jsonl", "transcript_full.jsonl"}:
        sibling_name = "transcript_full.jsonl" if source.name == "transcript.jsonl" else "transcript.jsonl"
        sibling = source.with_name(sibling_name)
        if sibling.exists():
            files.append((sibling, "."))

        # Include artifacts in brain folder if present
        conv_dir = source.parent.parent.parent
        if conv_dir.exists():
            for item in conv_dir.iterdir():
                if item.is_file() and item.suffix.lower() in {".md", ".json", ".txt", ".png", ".jpg", ".webp"}:
                    files.append((item, "artifacts"))

    return create_archive("antigravity", archive, files)


def import_source_files(archive: Path, root: Path) -> list[Path]:
    destination = display_root(root) / "imported"
    return inject_archive("antigravity", archive, destination)
