"""Local viewer for GitHub Copilot session-state folders.

Run with: python session_token_viewer.py
Then open http://127.0.0.1:8765 in a browser.
"""
from __future__ import annotations

import argparse
import html
import json
import logging
import os
import re
import sqlite3
import threading
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TypedDict
from urllib.parse import parse_qs, unquote, urlencode, urlparse

import anthropic_claude_provider
import github_copilot_provider
import m365_copilot_provider
import openai_codex_provider

TOKEN_KEYS = (
    "inputTokens",
    "cacheReadTokens",
    "cacheWriteTokens",
    "outputTokens",
    "reasoningTokens",
)
TOKEN_LABELS = {
    "inputTokens": "Input",
    "cacheReadTokens": "Input cache read",
    "cacheWriteTokens": "Input cache write",
    "outputTokens": "Output",
    "reasoningTokens": "Output reasoning",
}
PROVIDERS = {
    "copilot": "GitHub Copilot",
    "codex": "OpenAI Codex",
    "claude": "Claude Code",
    "m365_copilot": "Microsoft 365 Copilot",
}
PROVIDER_ADAPTERS = {
    "copilot": github_copilot_provider,
    "codex": openai_codex_provider,
    "claude": anthropic_claude_provider,
    "m365_copilot": m365_copilot_provider,
}
LOGGER = logging.getLogger(__name__)
TOKEN_ALIASES = {
    "inputTokens": ("input_tokens", "prompt_tokens", "promptTokens", "inputTokens"),
    "cacheReadTokens": ("cached_tokens", "cachedTokens", "input_cached_tokens", "cached_input_tokens", "cache_read_input_tokens", "cacheReadTokens"),
    "cacheWriteTokens": ("cache_write_input_tokens", "input_cache_write_tokens", "cache_creation_input_tokens", "cacheWriteTokens"),
    "outputTokens": ("output_tokens", "completion_tokens", "outputTokens"),
    "reasoningTokens": ("reasoning_output_tokens", "reasoning_tokens", "reasoningTokens"),
}
class SessionData(TypedDict, total=False):
    """Provider-neutral conversation contract returned to the UI layer."""

    id: str
    name: str
    updated: float
    turns: list[dict]
    tokens: dict[str, int | None]
    model: str | None
    project: str | None
    source: str
    _source: Path
    _kind: str
    _source_label: str
    provider: str


def normalize_session_data(value: dict) -> SessionData:
    """Ensure every provider returns the same conversation shape."""
    tokens = blank_tokens()
    supplied_tokens = value.get("tokens")
    if isinstance(supplied_tokens, dict):
        for key in TOKEN_KEYS:
            candidate = supplied_tokens.get(key)
            if isinstance(candidate, int) and not isinstance(candidate, bool):
                tokens[key] = candidate
    result: SessionData = {
        "id": str(value.get("id", "")),
        "name": str(value.get("name") or value.get("id", "")),
        "updated": float(value.get("updated") or 0),
        "turns": value.get("turns") if isinstance(value.get("turns"), list) else [],
        "tokens": tokens,
        "model": value.get("model") if isinstance(value.get("model"), str) else None,
        "project": value.get("project") if isinstance(value.get("project"), str) else None,
    }
    for key in ("source", "_source", "_kind", "_source_label", "_session_id", "_db_metadata", "_has_data", "provider"):
        if key in value:
            result[key] = value[key]
    return result


def session_has_content(session: dict) -> bool:
    """Return whether a parsed conversation contains usable interaction data."""
    for turn in session.get("turns", []):
        if turn.get("user") or turn.get("assistant"):
            return True
        tokens = turn.get("tokens", {})
        if any(value is not None and value > 0 for value in tokens.values() if isinstance(value, int)):
            return True
    tokens = session.get("tokens", {})
    return any(value is not None and value > 0 for value in tokens.values() if isinstance(value, int))


def number(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def conversation_name(name: object, session_id: str) -> str:
    value = str(name or "").strip()
    if not value or value.lstrip("# ").startswith("rollout-"):
        return session_id
    return value


def blank_tokens() -> dict[str, int | None]:
    return {key: None for key in TOKEN_KEYS}


def new_session(session_id: str, name: str, updated: float, model: str | None = None, project: str | None = None) -> dict:
    return {"id": session_id, "name": name, "updated": updated, "turns": [], "tokens": blank_tokens(), "model": model, "project": project}


def new_turn(turn_id: str) -> dict:
    return {"id": turn_id, "user": "", "assistant": [], "tokens": blank_tokens(), "raw": []}


def add_token_usage(tokens: dict[str, int | None], source: dict) -> None:
    for key in TOKEN_KEYS:
        value = number(source.get(key))
        if value is not None:
            tokens[key] = (tokens[key] or 0) + value


def subtract_cached_input(session: dict) -> None:
    """Convert total input into uncached input when cache usage is reported."""
    for tokens in [session.get("tokens", {})] + [turn.get("tokens", {}) for turn in session.get("turns", [])]:
        input_tokens = tokens.get("inputTokens")
        cache_read = tokens.get("cacheReadTokens")
        cache_write = tokens.get("cacheWriteTokens")
        cached_tokens = (cache_read or 0) + (cache_write or 0)
        if input_tokens is not None and (cache_read is not None or cache_write is not None):
            tokens["inputTokens"] = max(0, input_tokens - cached_tokens)


def usage_from(source: object) -> dict[str, int | None]:
    if not isinstance(source, dict):
        return blank_tokens()
    # Claude may put cache creation and reasoning details in nested usage
    # objects. Normalize those fields before applying the provider aliases.
    normalized = dict(source)
    cache_creation = source.get("cache_creation")
    if isinstance(cache_creation, dict) and "cache_creation_input_tokens" not in normalized:
        normalized["cache_creation_input_tokens"] = cache_creation.get("input_tokens")
    output_details = source.get("output_tokens_details")
    if isinstance(output_details, dict) and "reasoning_tokens" not in normalized:
        normalized["reasoning_tokens"] = output_details.get("reasoning_tokens")
    result = blank_tokens()
    for key, aliases in TOKEN_ALIASES.items():
        result[key] = next(
            (value for name in aliases if (value := number(normalized.get(name))) is not None),
            None,
        )
    return result


def safe_json_lines(path: Path) -> list[dict]:
    records: list[dict] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        for line_number, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                LOGGER.warning("Skipping malformed JSON in %s at line %d", path, line_number)
                continue
            if isinstance(record, dict):
                records.append(record)
            else:
                LOGGER.warning("Skipping non-object JSON in %s at line %d", path, line_number)
        # Some exports contain one JSON array rather than newline-delimited
        # records. Only try this fallback when JSONL parsing produced nothing.
        if not records and text.strip():
            document = json.loads(text)
            if isinstance(document, list):
                records = [record for record in document if isinstance(record, dict)]
    except FileNotFoundError:
        # Session files can be removed by VS Code while the directory is
        # being scanned (for example when a chat session is closed). Treat a
        # disappeared file as an empty session instead of printing a noisy
        # traceback for an expected filesystem race.
        LOGGER.debug("Session file disappeared before it could be read: %s", path)
    except json.JSONDecodeError:
        LOGGER.warning("Unable to parse JSON export %s", path)
    except OSError as error:
        LOGGER.warning("Unable to read %s: %s", path, error)
    return records


def project_path(value: object) -> str | None:
    """Normalize a local path or VS Code file URI for display."""
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    if value.startswith("file://"):
        parsed = urlparse(value)
        value = unquote(parsed.path)
        if re.match(r"^/[A-Za-z]:/", value):
            value = value[1:]
        if parsed.netloc and parsed.netloc != "localhost":
            value = f"//{parsed.netloc}{value}"
        if os.name == "nt":
            value = value.replace("/", "\\")
    return os.path.normpath(value)


def project_from_records(records: list[dict]) -> str | None:
    """Find common working-directory fields without exposing unrelated metadata."""
    keys = {
        "cwd", "workingDirectory", "working_directory", "projectPath", "project_path",
        "projectDirectory", "project_dir", "workspaceFolder", "workspace_path",
        "directory", "folder",
    }

    def visit(value: object) -> str | None:
        if isinstance(value, dict):
            for key in keys:
                result = project_path(value.get(key))
                if result:
                    return result
            for child in value.values():
                result = visit(child)
                if result:
                    return result
        elif isinstance(value, list):
            for child in value:
                result = visit(child)
                if result:
                    return result
        return None

    return visit(records)


def workspace_project_path(folder: Path) -> str | None:
    metadata_path = folder / "workspace.json"
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(metadata, dict):
        return project_path(metadata.get("folder") or metadata.get("workspaceFolder") or metadata.get("path"))
    return None


def parse_session(folder: Path) -> dict:
    try:
        updated = folder.stat().st_mtime
    except OSError:
        updated = 0
    session = {
        "id": folder.name,
        "name": folder.name,
        "updated": updated,
        "turns": [],
        "tokens": blank_tokens(),
        "model": None,
        "project": None,
    }
    workspace = folder / "workspace.yaml"
    if workspace.exists():
        try:
            workspace_lines = workspace.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            workspace_lines = []
        for line in workspace_lines:
            if line.startswith("name:"):
                session["name"] = line.partition(":")[2].strip().strip('"\'') or folder.name
            elif line.startswith(("path:", "folder:", "workspace:")):
                session["project"] = project_path(line.partition(":")[2].strip().strip('"\''))

    events = folder / "events.jsonl"
    if not events.exists():
        return session

    records = safe_json_lines(events)
    session["project"] = project_from_records(records)
    turns: dict[str, dict] = {}
    turn_interactions: dict[str, str] = {}
    message_output_total = 0

    def get_turn(key: str) -> dict:
        return turns.setdefault(key, new_turn(key))
    for event in records:
        if not isinstance(event, dict):
            continue
        event_type = event.get("type", "")
        data = event.get("data") or {}
        if not isinstance(data, dict):
            continue
        turn_id = str(data.get("turnId", ""))
        interaction_id = str(data.get("interactionId") or turn_interactions.get(turn_id, ""))
        if not interaction_id and event_type in {"assistant.turn_start", "user.message", "assistant.message"}:
            interaction_id = turn_id or f"turn-{len(turns) + 1}"
        if interaction_id:
            get_turn(interaction_id)["raw"].append(json.dumps(event, indent=2, ensure_ascii=False))
        if event_type == "session.start":
            session["model"] = data.get("selectedModel")
        elif event_type == "assistant.turn_start":
            interaction_id = interaction_id or turn_id
            turn_interactions[turn_id] = interaction_id
            get_turn(interaction_id)
        elif event_type == "user.message":
            # One interaction is one user input plus all assistant output
            # generated for it, including tool calls and follow-up turns.
            get_turn(interaction_id)["user"] = str(data.get("content", ""))
        elif event_type == "assistant.message":
            turn = get_turn(interaction_id)
            content = data.get("content") or ""
            if content:
                turn["assistant"].append(str(content))
            add_token_usage(turn["tokens"], data)
            value = number(data.get("outputTokens"))
            if value is not None:
                message_output_total += value
        elif event_type == "session.shutdown":
            metrics = data.get("modelMetrics") or {}
            if isinstance(metrics, dict):
                for model_name, model in metrics.items():
                    if not isinstance(model, dict):
                        continue
                    session["model"] = session["model"] or str(model_name)
                    usage = model.get("usage") or {}
                    if isinstance(usage, dict):
                        for key, value in usage_from(usage).items():
                            if value is not None:
                                session["tokens"][key] = (session["tokens"][key] or 0) + value

    session["turns"] = list(turns.values())
    # assistant.message events include output usage for every interaction,
    # including messages appended after a resume. Prefer their sum over an
    # older session.shutdown aggregate so the total covers the whole file.
    if message_output_total:
        session["tokens"]["outputTokens"] = message_output_total
    # Copilot currently records input/cache/reasoning usage in the
    # session.shutdown aggregate, not on individual assistant.message events.
    # For a single-interaction session the aggregate belongs unambiguously to
    # that turn, so expose it there too. Multi-interaction sessions retain —
    # rather than displaying misleading duplicated totals.
    if len(session["turns"]) == 1:
        turn_tokens = session["turns"][0]["tokens"]
        for key in TOKEN_KEYS:
            if turn_tokens[key] is None:
                turn_tokens[key] = session["tokens"][key]
    elif session["turns"]:
        # The source format exposes these four values only as session totals.
        # Distribute them by each turn's output-token share so every turn has
        # a useful estimate while preserving the exact session total.
        weights = [max(turn["tokens"]["outputTokens"] or 0, 1) for turn in session["turns"]]
        weight_total = sum(weights)
        for key in ("inputTokens", "cacheReadTokens", "cacheWriteTokens", "reasoningTokens"):
            total = session["tokens"][key]
            if total is None:
                continue
            assigned = 0
            for index, turn in enumerate(session["turns"]):
                if index == len(session["turns"]) - 1:
                    value = total - assigned
                else:
                    value = int(total * weights[index] / weight_total)
                    assigned += value
                turn["tokens"][key] = value
    return session


def first_json_record(path: Path) -> dict:
    try:
        with path.open(encoding="utf-8", errors="replace") as file:
            for line in file:
                if line.strip():
                    record = json.loads(line)
                    return record if isinstance(record, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}
    return {}


def latest_chat_field(path: Path, field: str) -> object:
    value: object = None
    try:
        with path.open(encoding="utf-8", errors="replace") as file:
            for line in file:
                if not line.strip():
                    continue
                record = json.loads(line)
                if isinstance(record, dict) and record.get("k") == [field]:
                    value = record.get("v")
    except (OSError, json.JSONDecodeError):
        return value
    return value


def session_summary(path: Path, provider: str, kind: str) -> dict:
    try:
        updated = path.stat().st_mtime
    except OSError:
        updated = 0
    summary = new_session(path.stem, path.stem, updated, provider)
    summary["_source"] = path
    summary["_kind"] = kind
    summary["_source_label"] = {
        "copilot-chat": "VS Code",
        "copilot-legacy": "Copilot legacy",
        "external": PROVIDERS.get(provider, provider),
    }.get(kind, PROVIDERS.get(provider, provider))
    if kind == "copilot-chat":
        metadata = first_json_record(path).get("v", {})
        if isinstance(metadata, dict):
            summary["id"] = str(metadata.get("sessionId") or path.stem)
            summary["name"] = str(metadata.get("customTitle") or summary["id"])
            summary["project"] = project_path(metadata.get("folder") or metadata.get("workspaceFolder") or metadata.get("projectPath"))
            if not summary["project"]:
                summary["project"] = workspace_project_path(path.parent.parent)
            updated_title = latest_chat_field(path, "customTitle")
            if updated_title:
                summary["name"] = str(updated_title)
            requests = metadata.get("requests", [])
            if not requests:
                summary["_has_data"] = False
            elif isinstance(requests, list):
                summary["_has_data"] = any(
                    isinstance(request, dict)
                    and (isinstance(request.get("message"), dict) and request["message"].get("text")
                        or request.get("promptTokens") is not None
                        or request.get("completionTokens") is not None
                         or any(isinstance(item, dict) and item.get("value") for item in (request.get("response") or [])))
                    for request in requests
                )
                # Newer VS Code versions persist request results as patch
                # records after the metadata record. The metadata request
                # entries can therefore look empty even when the transcript
                # contains a completed response.
                if not summary["_has_data"]:
                    for record in safe_json_lines(path):
                        key = record.get("k")
                        value = record.get("v")
                        has_completion = key[2] == "completionTokens" and number(value) is not None if isinstance(key, list) and len(key) >= 3 else False
                        has_message = (key[2] == "message" and isinstance(value, dict) and value.get("text")
                                       if isinstance(key, list) and len(key) >= 3 else False)
                        has_response = (key[2] == "response" and isinstance(value, list)
                                        and any(isinstance(item, dict) and item.get("value") for item in value)
                                        if isinstance(key, list) and len(key) >= 3 else False)
                        if (isinstance(key, list) and len(key) >= 3
                                and key[0] == "requests"
                                and (has_completion or has_message or has_response)):
                            summary["_has_data"] = True
                            break
    elif kind == "copilot-legacy":
        workspace = path / "workspace.yaml"
        try:
            for line in workspace.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("name:"):
                    summary["name"] = line.partition(":")[2].strip().strip('"\'') or path.name
                elif line.startswith(("path:", "folder:", "workspace:")):
                    summary["project"] = project_path(line.partition(":")[2].strip().strip('"\''))
        except OSError:
            pass
    elif kind == "external":
        first = first_json_record(path)
        session_id, display_name = PROVIDER_ADAPTERS[provider].identity(first, path.stem)
        if session_id:
            summary["id"] = str(session_id)
            summary["name"] = display_name
        summary["project"] = project_from_records([first])
    return summary


def parse_export_session(path: Path, provider: str) -> dict:
    try:
        updated = path.stat().st_mtime
    except OSError:
        updated = 0
    session = new_session(path.stem, path.stem, updated)
    turns: dict[str, dict] = {}
    records = safe_json_lines(path)
    session["project"] = project_from_records(records)
    if provider == "claude" and session.get("project"):
        normalized_project = session["project"].replace("/", "\\").lower()
        if "\\local-agent-mode-sessions\\" in normalized_project:
            # Claude Desktop's cwd is its private per-session output folder,
            # not a user project directory.
            session["project"] = None
    if not session["project"] and provider == "claude" and path.parent.name.startswith(("c--", "d--")):
        encoded = path.parent.name
        session["project"] = encoded.replace("--", ":\\", 1).replace("-", "\\")
    if records and isinstance(records[0], dict):
        first = records[0]
        session["id"], session["name"] = PROVIDER_ADAPTERS[provider].identity(first, session["id"])

    def get_turn(key: str) -> dict:
        return turns.setdefault(key, new_turn(key))

    current_turn = ""
    parse_records: list[dict] = []
    for record in records:
        messages = record.get("messages")
        if isinstance(messages, list):
            for index, message_record in enumerate(messages, start=1):
                if isinstance(message_record, dict):
                    item = dict(message_record)
                    item.setdefault("turn_id", item.get("turnId") or record.get("conversationId") or f"turn-{index}")
                    parse_records.append(item)
        else:
            parse_records.append(record)
    for record in parse_records:
        payload = record.get("payload", record)
        if not isinstance(payload, dict):
            continue
        message = record.get("message") if isinstance(record.get("message"), dict) else {}
        item = payload.get("item") if isinstance(payload.get("item"), dict) else {}
        metadata = payload.get("internal_chat_message_metadata_passthrough") or item.get("internal_chat_message_metadata_passthrough") or {}
        if not isinstance(metadata, dict):
            metadata = {}
        explicit_turn_id = payload.get("turn_id") or payload.get("turnId") or metadata.get("turn_id") or item.get("turn_id") or record.get("promptId")
        if not explicit_turn_id and record.get("type") == "user":
            explicit_turn_id = record.get("uuid")
        if not explicit_turn_id and not current_turn and record.get("type") in {"queue-operation", "attachment"}:
            continue
        if not explicit_turn_id and not current_turn and (payload.get("type") or record.get("type")) in {"session_meta", "world_state", "turn_context"}:
            continue
        turn_id = str(explicit_turn_id or current_turn)
        if not turn_id:
            # Session metadata and global records are attached to the latest turn.
            turn_id = str(record.get("timestamp") or len(turns))
        current_turn = turn_id
        raw = json.dumps(record, indent=2, ensure_ascii=False)
        turn = get_turn(turn_id)
        turn["raw"].append(raw)
        author = payload.get("author") or message.get("author") or item.get("author") or {}
        if not isinstance(author, dict):
            author = {}
        role = payload.get("role") or message.get("role") or item.get("role") or author.get("role")
        content = (payload.get("content") or message.get("content") or item.get("content")
                   or payload.get("text") or item.get("text") or payload.get("last_agent_message"))
        if isinstance(content, list):
            content = "\n".join(str(part.get("text", part)) if isinstance(part, dict) else str(part) for part in content)
        if content and role == "user":
            # Provider transcripts can include injected context (system
            # instructions, plugin lists, and environment state) as earlier
            # user-role records. Keep the latest actual prompt in the turn;
            # all records remain available through the raw explorer.
            turn["user"] = str(content)
        elif content and role in {"assistant", "model"}:
            turn["assistant"].append(str(content))
        info = payload.get("info") or {}
        if not isinstance(info, dict):
            info = {}
        usage = payload.get("usage") or message.get("usage") or payload.get("usageMetadata") or info.get("last_token_usage") or {}
        if isinstance(usage, dict):
            for key, value in usage_from(usage).items():
                if value is not None:
                    turn["tokens"][key] = (turn["tokens"][key] or 0) + value
    session["turns"] = list(turns.values())
    model = next(
        (
            candidate
            for record in records
            for candidate in (
                record.get("model"),
                (record.get("message") or {}).get("model") if isinstance(record.get("message"), dict) else None,
                (record.get("payload") or {}).get("model") if isinstance(record.get("payload"), dict) else None,
            )
            if isinstance(candidate, str) and candidate
        ),
        None,
    )
    session["model"] = model or provider
    for key in TOKEN_KEYS:
        values = [turn["tokens"][key] for turn in session["turns"] if turn["tokens"][key] is not None]
        if values:
            session["tokens"][key] = sum(values)
    return session


def read_session(folder: Path) -> dict:
    """Compatibility entry point for the Copilot legacy parser."""
    return parse_session(folder)


def read_external_session(path: Path, provider: str) -> dict:
    """Compatibility entry point for Codex and Claude transcript parsing."""
    return parse_export_session(path, provider)


def load_session_index(root: Path, provider: str, show_empty: bool = False) -> list[dict]:
    """Load inexpensive provider summaries for the sidebar."""
    adapter = PROVIDER_ADAPTERS[provider]
    normalized = []
    for item in adapter.index(root):
        item["provider"] = provider
        normalized.append(normalize_session_data(item))
    if show_empty:
        return normalized
    visible = []
    for item in normalized:
        if item.get("_has_data") is False:
            continue
        # Some providers cannot determine emptiness from their cheap metadata
        # scan. Parse those summaries lazily so the toggle has consistent
        # behavior across every provider.
        if item.get("_has_data") is not True:
            if not session_has_content(adapter.details(item)):
                continue
        visible.append(item)
    return visible


def load_session_details(summary: dict, provider: str) -> dict:
    """Load one full transcript through its provider adapter."""
    return normalize_session_data(PROVIDER_ADAPTERS[provider].details(summary))


def delete_session(summary: dict) -> None:
    """Delete a session using the owning provider's storage rules."""
    provider = summary.get("provider") or summary.get("_provider")
    if not isinstance(provider, str) or provider not in PROVIDER_ADAPTERS:
        raise ValueError("Session summary does not identify its provider")
    PROVIDER_ADAPTERS[provider].delete(summary)


def provider_path(root: Path, provider: str) -> Path:
    return PROVIDER_ADAPTERS[provider].display_root(root)


def fmt(value: int | None) -> str:
    return "—" if value is None else f"{value:,}"


def format_timestamp(value: float) -> str:
    if not value:
        return "Unknown time"
    return datetime.fromtimestamp(value).astimezone().strftime("%Y-%m-%d %H:%M")


def esc(value: object, quote: bool = True) -> str:
    return html.escape(str(value), quote=quote)


def token_cards(tokens: dict[str, int | None], extra_class: str = "") -> str:
    return "".join(
        f'<div class="metric {extra_class}"><span>{esc(TOKEN_LABELS[key])}</span><strong>{fmt(tokens.get(key))}</strong></div>'
        for key in TOKEN_KEYS
    )


def turn_token_cards(tokens: dict[str, int | None], session_id: str, provider: str, turn_index: int, selected_turn: int | None, selected_metric: str | None, show_empty: bool = False) -> str:
    cards = []
    for key in TOKEN_KEYS:
        active = selected_turn == turn_index and selected_metric == key
        href = "/?" + esc(urlencode({
            "provider": provider,
            "session": session_id,
            "turn": turn_index,
            "metric": key,
            "show_empty": int(show_empty),
        }), quote=True)
        cards.append(
            f'<a class="metric compact clickable {"active" if active else ""}" href="{href}">'
            f'<span>{esc(TOKEN_LABELS[key])}</span><strong>{fmt(tokens.get(key))}</strong></a>'
        )
    return "".join(cards)


def explorer_content(turn: dict, metric: str | None) -> tuple[str, str, str]:
    raw = "\n\n".join(turn.get("raw", []))
    if metric == "inputTokens":
        return "Input", turn["user"] or "(no user input)", raw
    if metric == "outputTokens":
        return "Output", "\n\n".join(turn["assistant"]) or "(no assistant output)", raw
    if metric in {"cacheReadTokens", "cacheWriteTokens", "reasoningTokens"}:
        return TOKEN_LABELS[metric], "Readable content is not stored for this token category in events.jsonl. Only the token count is available (the per-turn value is estimated when necessary).", raw
    return "Token content", "Click a token card in a turn to inspect its associated content.", ""


def render_session_row(item: dict, provider: str, selected: bool, show_empty: bool = False) -> str:
    query = esc(urlencode({"provider": provider, "session": item["id"], "show_empty": int(show_empty)}), quote=True)
    label = conversation_name(item.get("name"), item["id"])
    id_detail = "" if str(item["id"]).lower() in str(label).lower() else f'<small>{esc(item["id"])}</small>'
    metadata = (
        f'<small class="session-meta">{esc(item.get("_source_label", PROVIDERS.get(provider, provider)))} · '
        f'{esc(format_timestamp(item["updated"]))}</small>'
    )
    return (
        f'<div class="session-row"><a class="session session-link {"selected" if selected else ""}" href="/?{query}">'
        f'<span class="dot"></span><span><b>{esc(label)}</b>{id_detail}{metadata}</span></a>'
        f'<form method="post" action="/delete" onsubmit="return confirm(\'Delete this conversation and its stored data?\');">'
        f'<input type="hidden" name="provider" value="{esc(provider)}"><input type="hidden" name="session" value="{esc(item["id"])}">'
        f'<button class="delete-session" type="submit" title="Delete conversation" aria-label="Delete conversation">×</button></form></div>'
    )


def session_tool(summary: dict, provider: str) -> str:
    """Return the product surface reported by the owning provider."""
    return PROVIDER_ADAPTERS[provider].tool(summary)


def render(root: Path, selected: str | None, selected_turn: int | None = None, selected_metric: str | None = None, provider: str = "copilot", show_empty: bool = False) -> str:
    sessions = load_session_index(root, provider, show_empty)
    chosen_summary = next((item for item in sessions if item["id"] == selected), sessions[0] if sessions else None)
    chosen = load_session_details(chosen_summary, provider) if chosen_summary else None
    provider_menu = "".join(
        f'<a class="provider {"selected" if provider == key else ""}" href="/?{esc(urlencode({"provider": key, "show_empty": int(show_empty)}), quote=True)}">{esc(label)}</a>'
        for key, label in PROVIDERS.items()
    )
    session_rows = "".join(
        render_session_row(item, provider, bool(chosen_summary and item["id"] == chosen_summary["id"]), show_empty)
        for item in sessions
    )
    turns = ""
    if chosen:
        for index, turn in enumerate(chosen["turns"], start=1):
            assistant = "\n\n".join(turn["assistant"]) or "(no assistant text)"
            step_label = f' · Step {turn["step"]} of {turn["step_count"]}' if turn.get("step") else ""
            turn_label = turn.get("turn_index", index)
            turns += f'''<article class="turn"><header><b>Turn {esc(turn_label)}{esc(step_label)}</b></header>
                <div class="message user"><label>User</label><p>{esc(turn["user"] or "(no user message)")}</p></div>
                <div class="message assistant"><label>Assistant</label><p>{esc(assistant)}</p></div>
                <div class="turn-metrics">{turn_token_cards(turn["tokens"], chosen["id"], provider, index, selected_turn, selected_metric, show_empty)}</div></article>'''
    detail = ""
    if chosen:
        turn_note = " · input/cache/reasoning values estimated from session totals" if len(chosen["turns"]) > 1 else ""
        refresh_conversation_url = esc("/?" + urlencode({"provider": provider, "session": chosen["id"], "show_empty": int(show_empty)}), quote=True)
        explorer_title, explorer_text, explorer_raw = explorer_content(
            chosen["turns"][selected_turn - 1], selected_metric
        ) if selected_turn and 0 < selected_turn <= len(chosen["turns"]) else explorer_content({}, None)
        detail = f'''<section class="detail"><div class="eyebrow">{esc(PROVIDERS.get(provider, provider).upper())} · SESSION</div><div class="detail-heading"><div><h1>{esc(chosen["name"])}</h1>
            <div class="session-header-metadata"><span>Session:</span> {esc(chosen["id"])} <span>·</span> <span>Tool:</span> {esc(session_tool(chosen_summary, provider))} <span>·</span> <span>Timestamp:</span> {esc(format_timestamp(chosen.get("updated", chosen_summary.get("updated", 0))))} <span>·</span> <span>Model:</span> {esc(chosen["model"] or "unavailable")}</div>
            <div class="session-header-project"><span>Project Source:</span> {esc(chosen.get("project") or "unavailable")}</div>
            </div><div class="detail-actions"><a class="detail-refresh clickable" href="{refresh_conversation_url}">Refresh conversation</a><form class="detail-delete" method="post" action="/delete" onsubmit="return confirm('Delete this conversation and its stored data?');">
            <input type="hidden" name="provider" value="{esc(provider)}"><input type="hidden" name="session" value="{esc(chosen["id"])}">
            <button type="submit">Delete conversation</button></form></div></div>
            <div class="source-location"><span>Information Source:</span> {esc(chosen.get("source") or "unknown")}</div>
            <h2>Session token totals</h2><div class="metrics">{token_cards(chosen["tokens"])}</div>
            <h2>Turns <span class="muted">{len(chosen["turns"])}{esc(turn_note)}</span></h2>
            <div class="content-layout"><div class="turns">{turns or '<div class="empty">No turn events found.</div>'}</div>
            <aside class="explorer"><div class="eyebrow">CONTENT EXPLORER</div><h2>{esc(explorer_title)}</h2><p>{esc(explorer_text)}</p>{f'<details><summary>Raw event data</summary><pre>{esc(explorer_raw)}</pre></details>' if explorer_raw else ''}</aside></div></section>'''
    else:
        detail = '<section class="detail no-sessions"><h1>No sessions found</h1><p>Choose a folder containing session subfolders with events.jsonl files.</p></section>'

    refresh_url = esc("/?" + urlencode({"provider": provider, "show_empty": int(show_empty)}), quote=True)
    toggle_url = esc("/?" + urlencode({"provider": provider, "show_empty": int(not show_empty)}), quote=True)
    toggle = f'<a class="empty-toggle" href="{toggle_url}">{"Hide" if show_empty else "Show"} empty</a>'
    return PAGE.replace("__PROVIDER_MENU__", provider_menu).replace("__SESSION_ROWS__", session_rows).replace("__DETAIL__", detail).replace("__REFRESH_URL__", refresh_url).replace("__EMPTY_TOGGLE__", toggle).replace("__ROOT__", esc(provider_path(root, provider)))


class Handler(BaseHTTPRequestHandler):
    root = Path(".")

    def do_POST(self) -> None:
        if urlparse(self.path).path != "/delete":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        form = parse_qs(self.rfile.read(length).decode("utf-8", errors="replace"))
        provider = form.get("provider", ["copilot"])[0]
        session_id = form.get("session", [""])[0]
        if provider not in PROVIDERS or not session_id:
            self.send_error(400, "Invalid delete request")
            return
        summaries = load_session_index(self.root, provider, show_empty=True)
        summary = next((item for item in summaries if item["id"] == session_id), None)
        if summary is None:
            self.send_error(404, "Session not found")
            return
        try:
            delete_session(summary)
        except (OSError, sqlite3.Error) as error:
            LOGGER.warning("Unable to delete session %s: %s", session_id, error)
            self.send_error(500, "Unable to delete session")
            return
        self.send_response(303)
        self.send_header("Location", f"/?{urlencode({'provider': provider})}")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def do_GET(self) -> None:
        query = parse_qs(urlparse(self.path).query)
        provider = query.get("provider", ["copilot"])[0]
        if provider not in PROVIDERS:
            provider = "copilot"
        selected = query.get("session", [None])[0]
        turn_value = query.get("turn", [None])[0]
        selected_turn = int(turn_value) if turn_value and turn_value.isdigit() else None
        selected_metric = query.get("metric", [None])[0]
        show_empty = query.get("show_empty", ["0"])[0] in {"1", "true", "yes"}
        try:
            body = render(self.root, selected, selected_turn, selected_metric, provider, show_empty).encode("utf-8")
        except OSError:
            self.send_error(500, "Unable to read session files")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # Browsers can cancel a navigation while a lazy session is being
            # loaded. The client disconnect is not an application failure.
            return
    def log_message(self, *_: object) -> None:
        pass


def main() -> None:
    from github_copilot_provider import default_root as copilot_default_root
    parser = argparse.ArgumentParser(description="View Copilot session token usage")
    parser.add_argument("--root", type=Path, default=copilot_default_root(), help="Copilot/agent session root folder")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    Handler.root = args.root.expanduser().resolve()
    if not Handler.root.is_dir():
        parser.error(f"root must be an existing directory: {Handler.root}")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Viewing {Handler.root} at http://127.0.0.1:{args.port}")
    threading.Timer(0.3, lambda: webbrowser.open(f"http://127.0.0.1:{args.port}")).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


PAGE = r'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Session Token Viewer</title><style>
:root{font-family:Inter,ui-sans-serif,system-ui,sans-serif;color:#e7edf7;background:#0b1220}*{box-sizing:border-box}body{margin:0}.app{display:grid;grid-template-columns:340px 1fr;min-height:100vh}.sidebar{padding:28px 18px;background:#111b2d;border-right:1px solid #22304a}.brand{font-size:20px;font-weight:750;margin:0 10px 8px}.path{font-size:11px;color:#8fa2bf;margin:0 10px 24px;word-break:break-all}.provider-menu{display:grid;gap:4px;margin:0 0 25px}.provider{display:block;padding:9px 10px;color:#b8c9df;text-decoration:none;border-radius:8px;font-size:13px}.provider:hover,.provider.selected{background:#24558a;color:#fff}.count{font-size:11px;color:#8194b0;text-transform:uppercase;letter-spacing:1px;margin:0 10px 8px}.session-row{display:flex;align-items:stretch;gap:4px}.session-row form{display:flex;align-items:center;width:30px;flex:0 0 30px}.session{display:flex;flex:1;gap:11px;align-items:flex-start;padding:12px 10px;margin:3px 0;color:#cfdaea;text-decoration:none;border-radius:9px;min-width:0}.session:hover,.session.selected{background:#1b2b46}.session b{display:block;font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:250px}.session small{display:block;color:#8297b5;font-size:10px;margin-top:4px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.session .session-meta{color:#6380a2;font-size:9px;margin-top:3px}.delete-session{width:30px;height:30px;border:0;background:transparent;color:#7188a6;border-radius:6px;font-size:19px;line-height:1;padding:0;cursor:pointer}.delete-session:hover{background:#5b2534;color:#ffb4c0}.dot{width:7px;height:7px;background:#5ca8ff;border-radius:50%;margin-top:5px;flex:none}.detail{max-width:1500px;width:100%;padding:42px 4vw}.eyebrow{font-size:11px;letter-spacing:2px;color:#68b5ff;font-weight:700}h1{font-size:32px;margin:7px 0}.id{font:12px ui-monospace,monospace;color:#8094b2}.id span{padding:0 8px;color:#405576}h2{font-size:15px;margin:34px 0 12px}.muted{font-size:12px;color:#7890b1;font-weight:400}.metrics,.turn-metrics{display:grid;grid-template-columns:repeat(5,1fr);gap:10px}.metric{padding:15px;background:#121f34;border:1px solid #223753;border-radius:10px}.metric span{display:block;color:#91a5c3;font-size:11px}.metric strong{display:block;font-size:21px;margin-top:7px;color:#f0f6ff}.turn{background:#101c30;border:1px solid #223753;border-radius:12px;margin:12px 0;overflow:hidden}.turn header{padding:12px 16px;background:#15243b;font-size:13px}.message{padding:13px 16px;border-top:1px solid #1e304a}.message label{font-size:10px;text-transform:uppercase;letter-spacing:1px;color:#7fa6d2}.message p{white-space:pre-wrap;line-height:1.5;font-size:13px;margin:7px 0 0;color:#d4dfef}.message.user p{color:#abc7e8}.turn-metrics{padding:12px 16px;border-top:1px solid #1e304a}.metric.compact{padding:9px 10px}.clickable{display:block;color:inherit;text-decoration:none}.clickable:hover,.clickable.active{border-color:#59aaff;background:#18365a}.content-layout{display:grid;grid-template-columns:minmax(0,1fr) 340px;gap:22px;align-items:start}.explorer{position:sticky;top:24px;margin-top:12px;padding:18px;background:#111f34;border:1px solid #2c4666;border-radius:12px}.explorer h2{margin:8px 0 12px}.explorer p{white-space:pre-wrap;color:#c9d8eb;line-height:1.5;font-size:13px;margin:0}.explorer details{margin-top:18px;border-top:1px solid #2c4666;padding-top:12px}.explorer summary{cursor:pointer;color:#68b5ff;font-size:12px}.explorer pre{max-height:520px;overflow:auto;white-space:pre-wrap;word-break:break-word;color:#b9c9dd;font:10px ui-monospace,monospace;line-height:1.4}.empty{color:#8fa2bf;padding:28px;background:#101c30;border-radius:10px}@media(max-width:800px){.app{grid-template-columns:1fr}.sidebar{border-right:0;border-bottom:1px solid #22304a}.metrics,.turn-metrics{grid-template-columns:repeat(2,1fr)}.content-layout{grid-template-columns:1fr}.explorer{position:static}.detail{padding:32px 20px}}
</style><style>.loading{position:fixed;inset:0;background:rgba(11,18,32,.72);display:flex;align-items:center;justify-content:center;z-index:10;color:#dbeafe;font-size:14px}.spinner{width:24px;height:24px;border:3px solid #416080;border-top-color:#68b5ff;border-radius:50%;animation:spin .8s linear infinite;margin-right:10px}@keyframes spin{to{transform:rotate(360deg)}}.is-loading{pointer-events:none;opacity:.65}</style></head><body><div class="app"><aside class="sidebar"><div class="brand">Session explorer</div><div class="path">__ROOT__</div><nav class="provider-menu">__PROVIDER_MENU__</nav><div class="count">Sessions</div>__SESSION_ROWS__</aside>__DETAIL__</div><script>document.querySelectorAll('a.session-link,.clickable,.provider').forEach(function(link){link.addEventListener('click',function(){document.body.classList.add('is-loading');var overlay=document.createElement('div');overlay.className='loading';overlay.innerHTML='<span class="spinner"></span><span>Loading session data…</span>';document.body.appendChild(overlay);});});</script><script>document.querySelectorAll('form[action="/delete"]').forEach(function(form){form.addEventListener('submit',function(){document.body.classList.add('is-loading');var overlay=document.createElement('div');overlay.className='loading';overlay.innerHTML='<span class="spinner"></span><span>Deleting conversation…</span>';document.body.appendChild(overlay);form.querySelector('button').disabled=true;});});</script></body></html>'''


PAGE = PAGE.replace("</style></head>", r'''<style>
/* Keep long provider/session identifiers inside their cards. */
.session > span:last-child{min-width:0;flex:1;overflow:hidden}
.session b,.session small,.session .session-meta{max-width:100%;overflow:hidden;text-overflow:ellipsis}
.session-row{min-width:0;width:100%;overflow:hidden}
.id{overflow-wrap:anywhere;word-break:break-word}
.detail-heading{display:flex;align-items:flex-start;justify-content:space-between;gap:24px}
.detail-actions{display:flex;align-items:center;gap:8px;flex:none;margin-top:8px}
.detail-refresh{border:1px solid #315a82;background:#182e47;color:#a9d5ff;border-radius:7px;padding:8px 12px;font:inherit;font-size:12px;text-decoration:none;white-space:nowrap}
.detail-refresh:hover{background:#24558a;color:#fff}
.detail-delete{flex:none}
.detail-delete button{border:1px solid #7b3547;background:#3b1d2a;color:#ffbdc8;border-radius:7px;padding:8px 12px;font:inherit;font-size:12px;cursor:pointer}
.detail-delete button:hover{background:#5b2534;color:#fff}
.session-header-metadata{font:12px ui-monospace,monospace;color:#8094b2;overflow-wrap:anywhere;word-break:break-word}
.session-header-metadata span,.source-location span{color:#91a8c7;font-family:Inter,ui-sans-serif,system-ui,sans-serif}
.session-header-project{margin-top:6px;font:12px ui-monospace,monospace;color:#8094b2;overflow-wrap:anywhere;word-break:break-word}
.session-header-project span{color:#91a8c7;font-family:Inter,ui-sans-serif,system-ui,sans-serif}
.source-location{margin-top:8px;color:#7188a6;font:11px ui-monospace,monospace;overflow-wrap:anywhere;word-break:break-word}

/* Long GUIDs and raw payloads must never escape the content columns. */
.content-layout,.turns,.turn,.message,.explorer{min-width:0}
.turns > .empty{margin-top:12px}
.turn{overflow:hidden}
.message p,.explorer p{overflow-wrap:anywhere;word-break:break-word}
.explorer{overflow:hidden}
.explorer pre{max-width:100%;overflow:auto;white-space:pre-wrap;overflow-wrap:anywhere;word-break:break-word}

/* Slightly cleaner sidebar treatment. */
.app{height:100vh;min-height:100vh}
.sidebar{padding:24px 14px;background:linear-gradient(180deg,#121f35 0%,#0f192b 100%);display:flex;flex-direction:column;position:sticky;top:0;height:100vh;min-height:100vh;max-height:100vh;overflow:hidden}
.brand{font-size:18px;letter-spacing:.2px}
.provider-menu{padding:4px;background:#0c1627;border:1px solid #223451;border-radius:11px}
.provider{padding:10px 12px;border-radius:8px}
.provider.selected{background:#2c6aa5;box-shadow:0 4px 12px rgba(24,91,151,.25)}
.count{margin-top:22px;font-weight:700;color:#91a8c7}
.sessions-area{flex:1;min-height:0;min-width:0;overflow-y:auto;overflow-x:hidden;padding-right:4px;margin-right:-4px;scrollbar-width:thin;scrollbar-color:#416b98 #0c1627}
.sessions-area::-webkit-scrollbar{width:9px;height:0}
.sessions-area::-webkit-scrollbar-track{background:#0c1627;border-radius:8px}
.sessions-area::-webkit-scrollbar-thumb{background:#416b98;border:2px solid #0c1627;border-radius:8px}
.sessions-area::-webkit-scrollbar-thumb:hover{background:#68a4d8}
.session{padding:11px 9px;border:1px solid transparent;flex:1 1 auto;overflow:hidden}
.session:hover{border-color:#2e527a;background:#1a2b45}
.session.selected{border-color:#3b6b9d;background:#203653}
.session-row form{width:26px;flex-basis:26px}
.delete-session{width:26px;color:#8ba0bd}
.sessions-heading{display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:2;margin:0 0 8px;padding:8px 10px;background:#0f192b;border-bottom:1px solid #223451}
.sessions-heading .count{margin:0}
.sessions-heading-actions{display:flex;align-items:center;gap:6px}
.empty-toggle{color:#9eb5d0;font-size:10px;text-decoration:none;white-space:nowrap;padding:6px 7px;border:1px solid #2b4668;border-radius:7px;background:#172943}
.empty-toggle:hover{background:#24558a;color:#fff}
.refresh-sessions{display:inline-flex;align-items:center;justify-content:center;width:27px;height:27px;border:1px solid #2b4668;border-radius:7px;background:#172943;color:#a9c2df;text-decoration:none;font-size:16px;line-height:1;transition:background .15s,border-color .15s,color .15s}
.refresh-sessions:hover{background:#24558a;border-color:#4d8bc4;color:#fff}
/* Keep the sidebar outside the document scroll area on desktop. */
.app{display:block;height:auto;min-height:100vh}
.sidebar{position:fixed;left:0;top:0;width:340px;height:100vh;z-index:5}
.detail{margin-left:340px;width:calc(100% - 340px)}
@media(max-width:1000px){.sidebar{width:270px}.detail{margin-left:270px;width:calc(100% - 270px)}}
@media(max-width:700px){.app{display:block}.sidebar{position:static;width:auto;height:auto;min-height:auto;max-height:none}.detail{margin-left:0;width:100%}.sessions-area{overflow:visible;padding-right:0;margin-right:0}}
</style></head>''')

PAGE = PAGE.replace('<div class="count">Sessions</div>__SESSION_ROWS__', '<div class="sessions-area"><div class="sessions-heading"><div class="count">Sessions</div><div class="sessions-heading-actions">__EMPTY_TOGGLE__<a class="refresh-sessions" href="__REFRESH_URL__" title="Refresh sessions" aria-label="Refresh sessions">↻</a></div></div>__SESSION_ROWS__</div>')


if __name__ == "__main__":
    main()
