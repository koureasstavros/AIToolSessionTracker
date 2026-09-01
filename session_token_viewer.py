"""Local viewer for AI Tool Sessions.

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
import tempfile
import threading
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TypedDict
from urllib.parse import parse_qs, unquote, urlencode, urlparse

from src.providers import anthropic_claude_provider
from src.providers import github_copilot_provider
from src.providers import m365_copilot_provider
from src.providers import openai_codex_provider

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
    "claude": "Anthropic Claude Code",
    "m365_copilot": "Microsoft 365 Copilot",
}
APP_NAME = "AI Tool Session Explorer"
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
    for key in ("source", "_source", "_sources", "_kind", "_source_label", "_session_id", "_db_metadata", "_db_issue", "_has_data", "provider"):
        if key in value:
            result[key] = value[key]
    return result


def session_has_content(session: dict) -> bool:
    """Return whether a parsed conversation contains a meaningful turn."""
    for turn in session.get("turns", []):
        if turn.get("user") or turn.get("assistant"):
            return True
        tokens = turn.get("tokens", {})
        if any(
            isinstance(value, int) and not isinstance(value, bool)
            for value in tokens.values()
        ):
            return True
    tokens = session.get("tokens", {})
    return any(
        isinstance(value, int) and not isinstance(value, bool)
        for value in tokens.values()
    )


def number(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def conversation_name(name: object, session_id: str) -> str:
    value = str(name or "").strip()
    if not value or value.lstrip("# ").startswith("rollout-"):
        return session_id
    return value


def derived_conversation_name(records: list[dict], fallback: str) -> str:
    """Use the first real user message when a provider stores no title."""
    for record in records:
        payload = record.get("payload", record)
        if not isinstance(payload, dict):
            continue
        message = record.get("message") if isinstance(record.get("message"), dict) else {}
        role = payload.get("role") or message.get("role") or record.get("role")
        content = payload.get("content") or message.get("content") or record.get("message")
        if role != "user" or not content:
            continue
        parts = content if isinstance(content, list) else [content]
        text = "\n".join(
            str(part.get("text", ""))
            for part in parts
            if isinstance(part, dict) and part.get("type") != "tool_result" and part.get("text")
        ) if isinstance(content, list) else str(content)
        text = " ".join(text.split())
        if text.lower().startswith(("<environment_context>", "<system>", "<developer>")):
            continue
        if text:
            return text[:77].rstrip() + "..." if len(text) > 80 else text
    return fallback


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
    turn_tokens = [turn.get("tokens", {}) for turn in session.get("turns", [])]
    invocation_tokens = [
        invocation.get("tokens", {})
        for turn in session.get("turns", [])
        for invocation in turn.get("invocations", [])
        if isinstance(invocation, dict)
    ]
    for tokens in [session.get("tokens", {})] + turn_tokens + invocation_tokens:
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
        "copilot-chat": "Extension",
        "copilot-session-state": "CLI / Desktop",
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
    elif kind == "copilot-session-state":
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
            if summary["name"] == summary["id"]:
                summary["name"] = derived_conversation_name(safe_json_lines(path), summary["id"])
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
        if session["name"] == session["id"]:
            session["name"] = derived_conversation_name(records, session["id"])

    def get_turn(key: str) -> dict:
        return turns.setdefault(key, new_turn(key))

    current_turn = ""
    parse_records: list[dict] = []
    seen_usage_records: set[str] = set()
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
            # Claude Code may persist one assistant API response as separate
            # text and tool_use records. They share a message ID and usage,
            # so count the usage only once.
            message_id = message.get("id") if isinstance(message, dict) else None
            usage_id = message_id or record.get("requestId")
            if usage_id is None:
                usage_id = record.get("uuid")
            if usage_id is None or str(usage_id) not in seen_usage_records:
                if usage_id is not None:
                    seen_usage_records.add(str(usage_id))
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
    """Compatibility entry point for the Copilot session-state parser."""
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
    normalized.sort(key=lambda item: item.get("updated", 0), reverse=True)
    if show_empty:
        return normalized
    visible = []
    for item in normalized:
        # A provider's cheap scan may only know that usage statistics are
        # absent. Parse such summaries before hiding them so conversations
        # containing prompts or responses but no token statistics remain
        # visible.
        if item.get("_has_data") is not True and not session_has_content(adapter.details(item)):
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


def export_session_sources(summary: dict, provider: str, archive: Path) -> Path:
    """Export the owning provider's original source files."""
    return PROVIDER_ADAPTERS[provider].export_source_files(summary, archive)


def import_session_sources(provider: str, archive: Path, root: Path) -> list[Path]:
    """Inject an archive into the owning provider's native local storage."""
    return PROVIDER_ADAPTERS[provider].import_source_files(archive, root)


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
        f'<div class="metric {extra_class}" data-metric="{esc(key)}"><span>{esc(TOKEN_LABELS[key])}</span><strong>{fmt(tokens.get(key))}</strong></div>'
        for key in TOKEN_KEYS
    )


def invocation_token_cards(tokens: dict[str, int | None]) -> str:
    """Render per-invocation usage grouped by the side of the model exchange."""
    def cards(keys: tuple[str, ...]) -> str:
        return "".join(
            f'<div class="metric compact" data-metric="{esc(key)}"><span>{esc(TOKEN_LABELS[key])}</span><strong>{fmt(tokens.get(key))}</strong></div>'
            for key in keys
        )

    return (
        '<div class="invocation-usage">'
        '<section class="invocation-metric-group input-group"><div class="invocation-group-title"><span>U</span>User / input</div>'
        f'<div class="invocation-metrics">{cards(("inputTokens", "cacheReadTokens", "cacheWriteTokens"))}</div></section>'
        '<section class="invocation-metric-group output-group"><div class="invocation-group-title"><span>AI</span>Assistant / output</div>'
        f'<div class="invocation-metrics">{cards(("outputTokens", "reasoningTokens"))}</div></section>'
        '</div>'
    )


def turn_token_cards(tokens: dict[str, int | None], session_id: str, provider: str, turn_index: int, selected_turn: int | None, selected_metric: str | None, show_empty: bool = False) -> str:
    cards = []
    for key in TOKEN_KEYS:
        active = selected_turn == turn_index and selected_metric == key
        href = "/?" + esc(urlencode({
            "provider": provider,
            "show_empty": int(show_empty),
            "session": session_id,
            "turn": turn_index,
            "metric": key,
        }), quote=True)
        cards.append(
            f'<a class="metric compact clickable {"active" if active else ""}" data-metric="{esc(key)}" href="{href}">'
            f'<span>{esc(TOKEN_LABELS[key])}</span><strong>{fmt(tokens.get(key))}</strong></a>'
        )
    return "".join(cards)


def invocation_tools(invocation: dict) -> str:
    """Render the tool calls that belong to one model invocation."""
    tools = invocation.get("tools") if isinstance(invocation.get("tools"), list) else []
    if not tools:
        return '<div class="invocation-no-tools">Model response · no tool calls</div>'
    rendered = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        name = tool.get("name") or "unknown"
        status = tool.get("status") or "unknown"
        sections = []
        for label, field in (("Arguments", "arguments"), ("Result", "result")):
            value = tool.get(field)
            if value is None:
                continue
            text = value if isinstance(value, str) else json.dumps(value, indent=2, ensure_ascii=False)
            sections.append(f'<div class="tool-payload"><b>{label}</b><pre>{esc(text)}</pre></div>')
        rendered.append(
            f'<details class="invocation-tool"><summary><span class="tool-name">{esc(name)}</span>'
            f'<span class="tool-status {esc(status)}">{esc(status)}</span></summary>'
            f'<div class="invocation-tool-body">{"".join(sections) or "No stored arguments or result."}</div></details>'
        )
    return '<div class="invocation-tools">' + "".join(rendered) + '</div>'


def explorer_content(turn: dict, metric: str | None) -> tuple[str, str, str]:
    raw = "\n\n".join(turn.get("raw", []))
    tools = turn.get("tools") if isinstance(turn.get("tools"), list) else []

    def tool_content(field: str) -> str:
        parts = []
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            value = tool.get(field)
            if value is None:
                continue
            rendered = value if isinstance(value, str) else json.dumps(value, indent=2, ensure_ascii=False)
            parts.append(f'{tool.get("name") or "unknown"}:\n{rendered}')
        return "\n\n".join(parts)

    if metric == "inputTokens":
        return "Input", turn["user"] or tool_content("arguments") or "(no user input)", raw
    if metric == "outputTokens":
        return "Output", "\n\n".join(turn["assistant"]) or tool_content("result") or "(no assistant output)", raw
    if metric in {"cacheReadTokens", "cacheWriteTokens", "reasoningTokens"}:
        return TOKEN_LABELS[metric], "Readable content is not stored for this token category in events.jsonl. Only the token count is available (the per-turn value is estimated when necessary).", raw
    return "Token content", "Click a token card in a turn to inspect its associated content.", ""


def render_session_row(item: dict, provider: str, selected: bool, show_empty: bool = False) -> str:
    query = esc(urlencode({"provider": provider, "show_empty": int(show_empty), "session": item["id"]}), quote=True)
    label = conversation_name(item.get("name"), item["id"])
    id_detail = "" if str(item["id"]).lower() in str(label).lower() else f'<small>{esc(item["id"])}</small>'
    metadata = (
        f'<small class="session-meta">{esc(item.get("_source_label", PROVIDERS.get(provider, provider)))} · '
        f'{esc(format_timestamp(item["updated"]))}</small>'
    )
    return (
        f'<div class="session-row"><a class="session session-link {"selected" if selected else ""}" href="/?{query}">'
        f'<span class="session-glyph" aria-hidden="true">{esc(str(label)[:1].upper() or "S")}</span><span class="session-copy"><b>{esc(label)}</b>{id_detail}{metadata}</span></a>'
        f'<form method="post" action="/delete" onsubmit="return confirm(\'Delete this conversation and its stored data?\');">'
        f'<input type="hidden" name="provider" value="{esc(provider)}"><input type="hidden" name="show_empty" value="{int(show_empty)}">'
        f'<input type="hidden" name="session" value="{esc(item["id"])}">'
        f'<button class="delete-session" type="submit" title="Delete conversation" aria-label="Delete conversation">×</button></form></div>'
    )


def session_tool(summary: dict, provider: str) -> str:
    """Return the product surface reported by the owning provider."""
    return PROVIDER_ADAPTERS[provider].tool(summary)


def render(root: Path, selected: str | None, selected_turn: int | None = None, selected_metric: str | None = None, provider: str = "copilot", show_empty: bool = False, selected_raw: bool = False) -> str:
    sessions = load_session_index(root, provider, show_empty)
    chosen_summary = next((item for item in sessions if item["id"] == selected), None) if selected else None
    chosen = load_session_details(chosen_summary, provider) if chosen_summary else None
    provider_menu = "".join(
        f'<a class="provider {"selected" if provider == key else ""}" data-provider="{esc(key)}" href="/?{esc(urlencode({"provider": key, "show_empty": int(show_empty)}), quote=True)}"><span class="provider-mark" aria-hidden="true"></span><span>{esc(label)}</span></a>'
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
            tools = turn.get("tools") if isinstance(turn.get("tools"), list) else []
            invocations = turn.get("invocations") if isinstance(turn.get("invocations"), list) else []
            tools_are_nested = any(
                isinstance(invocation, dict) and isinstance(invocation.get("tools"), list) and invocation["tools"]
                for invocation in invocations
            )
            tool_markup_parts = []
            for tool in ([] if tools_are_nested else tools):
                if not isinstance(tool, dict):
                    continue
                details = ""
                if tool.get("arguments") is not None:
                    details += f'<br><code>{esc(json.dumps(tool["arguments"], ensure_ascii=False))}</code>'
                if tool.get("result") is not None:
                    details += f'<br>{esc(json.dumps(tool["result"], ensure_ascii=False))}'
                tool_markup_parts.append(
                    f'<div class="message tool"><label>Tool</label><p><b>{esc(tool.get("name") or "unknown")}</b> · '
                    f'{esc(tool.get("status") or "unknown")}{details}</p></div>'
                )
            tool_markup = "".join(tool_markup_parts)
            kind_label = (
                '<span class="turn-kind">Tool turn</span>' if turn.get("kind") == "tool" else
                '<span class="turn-kind">Usage summary</span>' if turn.get("kind") == "usage_summary" else
                ""
            )
            turn_label = turn.get("turn_index", index)
            raw_url = esc("/?" + urlencode({"provider": provider, "show_empty": int(show_empty), "session": chosen["id"], "turn": index, "raw": 1}), quote=True)
            invocation_markup = ""
            show_invocation_breakdown = len(invocations) > 1 or tools_are_nested
            if show_invocation_breakdown:
                invocation_markup = '<details class="turn-invocations" open><summary><span>Model invocations</span><span class="summary-count">' + str(len(invocations)) + '</span></summary><div class="invocations-list">' + "".join(
                    f'<div class="turn-invocation"><span class="invocation-name"><i></i>Invocation {esc(invocation.get("index", invocation_index))}'
                    f'{" <span class=\"turn-kind\">Usage summary</span>" if invocation.get("kind") == "usage_summary" else ""}'
                    f'</span><div class="invocation-content">{invocation_tools(invocation)}{invocation_token_cards(invocation.get("tokens", {}))}</div></div>'
                    for invocation_index, invocation in enumerate(invocations, 1) if isinstance(invocation, dict)
                ) + '</div></details>'
            invocation_label = f'{len(invocations)} {"invocation" if len(invocations) == 1 else "invocations"}'
            tool_label = f'{len(tools)} {"tool" if len(tools) == 1 else "tools"}'
            turns += f'''<article class="turn" id="turn-{index}"><header><div class="turn-number"><span>{index:02d}</span><div><b>Turn {esc(turn_label)}</b><small>{tool_label} · {invocation_label}</small></div></div><div class="turn-badges">{f'<span class="invocation-count">{invocation_label}</span>' if show_invocation_breakdown else ""}{kind_label}</div></header>
                <div class="message user"><div class="role"><span aria-hidden="true">U</span><label>User</label></div><p>{esc(turn["user"] or "(no user message)")}</p></div>
                <div class="message assistant"><div class="role"><span aria-hidden="true">AI</span><label>Assistant</label></div><p>{esc(assistant)}</p></div>
                {tool_markup}
                {invocation_markup}
                <div class="turn-footer"><div class="turn-metrics">{turn_token_cards(turn["tokens"], chosen["id"], provider, index, selected_turn, selected_metric, show_empty)}</div>
                <a class="show-raw clickable" href="{raw_url}"><span aria-hidden="true">&lt;/&gt;</span>View raw event data <span class="raw-arrow" aria-hidden="true">→</span></a></div></article>'''
    detail = ""
    if chosen:
        turn_note = " · input/cache/reasoning values estimated from session totals" if provider == "copilot" and len(chosen["turns"]) > 1 else ""
        refresh_conversation_url = esc("/?" + urlencode({"provider": provider, "show_empty": int(show_empty), "session": chosen["id"]}), quote=True)
        selected_content_turn = chosen["turns"][selected_turn - 1] if selected_turn and 0 < selected_turn <= len(chosen["turns"]) else {}
        if selected_raw:
            explorer_title, explorer_text, explorer_raw = "Raw event data", "", "\n\n".join(selected_content_turn.get("raw", []))
        else:
            explorer_title, explorer_text, explorer_raw = explorer_content(selected_content_turn, selected_metric)
        invocation_total = sum(len(turn.get("invocations", [])) for turn in chosen["turns"] if isinstance(turn.get("invocations"), list))
        # Reasoning tokens are already included in output tokens, so they are
        # not added again in the headline total.
        token_total = sum(
            chosen["tokens"].get(key) or 0
            for key in ("inputTokens", "cacheReadTokens", "cacheWriteTokens", "outputTokens")
        )
        close_explorer_url = refresh_conversation_url
        detail = f'''<main class="detail"><header class="detail-heading"><div class="heading-copy"><div class="eyebrow"><span></span>{esc(PROVIDERS.get(provider, provider))}</div><h1>{esc(chosen["name"])}</h1>
            <div class="header-chips"><span>{esc(session_tool(chosen_summary, provider))}</span><span>{esc(chosen["model"] or "Model unavailable")}</span><span>{esc(format_timestamp(chosen.get("updated", chosen_summary.get("updated", 0))))}</span></div>
            </div><div class="detail-actions"><a class="icon-button detail-refresh clickable" href="{refresh_conversation_url}" title="Refresh conversation" aria-label="Refresh conversation">↻</a><a class="session-export-button" href="/export?{esc(urlencode({'provider': provider, 'session': chosen['id']}), quote=True)}" title="Export this session's source files" aria-label="Export this session's source files">⇩</a><form class="detail-delete" method="post" action="/delete" onsubmit="return confirm('Delete this conversation and its stored data?');">
            <input type="hidden" name="provider" value="{esc(provider)}"><input type="hidden" name="show_empty" value="{int(show_empty)}"><input type="hidden" name="session" value="{esc(chosen["id"])}">
            <button type="submit" class="icon-button danger" title="Delete conversation" aria-label="Delete conversation">×</button></form></div></header>
            <section class="session-facts"><div><span>Session ID</span><code>{esc(chosen["id"])}</code></div><div><span>Project</span><code>{esc(chosen.get("project") or "Unavailable")}</code></div><div><span>Source</span><code>{esc(chosen.get("source") or "Unknown")}</code></div></section>
            {f'<div class="provider-note"><b>Provider note</b>{esc(chosen["_db_issue"])}</div>' if chosen.get("_db_issue") else ""}
            <section class="overview"><div class="section-heading"><div><span class="section-kicker">OVERVIEW</span><h2>Session usage</h2></div><div class="overview-stats"><span><b>{len(chosen["turns"])}</b> turns</span><span><b>{invocation_total}</b> invocations</span><span><b>{fmt(token_total)}</b> tokens</span></div></div><div class="metrics">{token_cards(chosen["tokens"])}</div></section>
            <section class="conversation"><div class="section-heading"><div><span class="section-kicker">TIMELINE</span><h2>Conversation turns</h2></div><span class="muted">{len(chosen["turns"])} turns{esc(turn_note)}</span></div>
            <div class="content-layout"><div class="turns">{turns or '<div class="empty"><b>No turns yet</b><span>No conversation events were found for this session.</span></div>'}</div>
            <aside class="explorer {"is-active" if selected_turn else ""}"><div class="explorer-header"><div><span class="section-kicker">INSPECTOR</span><h2>{esc(explorer_title)}</h2></div><a href="{close_explorer_url}" class="explorer-close" aria-label="Close inspector">×</a></div><div class="explorer-body">{f'<p>{esc(explorer_text)}</p>' if explorer_text else ''}{f'<pre>{esc(explorer_raw)}</pre>' if explorer_raw and selected_raw else ''}</div></aside></div></section></main>'''
    else:
        if sessions:
            detail = '<main class="detail no-sessions"><div class="empty-hero"><span class="hero-icon">↗</span><span class="section-kicker">__APP_NAME__</span><h1>Select a session</h1><p>Choose a conversation to inspect its turns, model invocations, token usage, tools, and raw events.</p></div></main>'
        else:
            detail = '<main class="detail no-sessions"><div class="empty-hero"><span class="hero-icon">○</span><span class="section-kicker">__APP_NAME__</span><h1>No sessions found</h1><p>No local conversations were found for this provider. Try showing empty sessions or refresh the source.</p></div></main>'

    refresh_url = esc("/?" + urlencode({"provider": provider, "show_empty": int(show_empty)}), quote=True)
    toggle_url = esc("/?" + urlencode({"provider": provider, "show_empty": int(not show_empty)}), quote=True)
    toggle_label = "Hide empty sessions" if show_empty else "Show empty sessions"
    toggle_icon = (
        '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12Z"></path><circle cx="12" cy="12" r="3"></circle></svg>'
        if show_empty else
        '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 3l18 18"></path><path d="M10.6 5.2A10.8 10.8 0 0 1 12 5c6.5 0 10 7 10 7a18 18 0 0 1-3.2 4.2"></path><path d="M6.2 6.2C3.5 8.1 2 12 2 12s3.5 7 10 7a10.8 10.8 0 0 0 3.4-.6"></path></svg>'
    )
    toggle = f'<a class="empty-toggle" href="{toggle_url}" title="{toggle_label}" aria-label="{toggle_label}">{toggle_icon}</a>'
    import_form = f'<form class="import-inline" method="post" action="/import" enctype="multipart/form-data"><input id="source-archive" name="archive" type="file" accept=".zip" required onchange="this.form.submit()"><input type="hidden" name="provider" value="{esc(provider)}"><label class="import-button" for="source-archive" title="Import one session archive" aria-label="Import one session archive">⇧</label></form>'
    return PAGE.replace("__APP_NAME__", esc(APP_NAME)).replace("__PROVIDER_MENU__", provider_menu).replace("__SESSION_ROWS__", session_rows).replace("__SESSION_COUNT__", str(len(sessions))).replace("__DETAIL__", detail).replace("__REFRESH_URL__", refresh_url).replace("__EMPTY_TOGGLE__", toggle).replace("__ROOT__", esc(provider_path(root, provider))).replace("__IMPORT_FORM__", import_form)


class Handler(BaseHTTPRequestHandler):
    root = Path(".")

    def do_POST(self) -> None:
        route = urlparse(self.path).path
        if route not in {"/delete", "/import"}:
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        if route == "/import":
            from email.parser import BytesParser
            from email.policy import default as email_policy
            content_type = self.headers.get("Content-Type", "")
            message = BytesParser(policy=email_policy).parsebytes(
                f"Content-Type: {content_type}\r\n\r\n".encode() + body
            )
            provider = ""
            upload = b""
            filename = "source.zip"
            for part in message.walk():
                if part.get_content_disposition() != "form-data":
                    continue
                field = part.get_param("name", header="content-disposition")
                if field == "provider":
                    provider = part.get_content().strip()
                elif field == "archive":
                    filename = part.get_filename() or filename
                    upload = part.get_payload(decode=True) or b""
            if provider not in PROVIDERS or not upload or not filename.lower().endswith(".zip"):
                self.send_error(400, "Invalid import request")
                return
            descriptor, temporary_name = tempfile.mkstemp(prefix="session-import-", suffix=".zip")
            os.close(descriptor)
            temporary = Path(temporary_name)
            try:
                temporary.write_bytes(upload)
                import_session_sources(provider, temporary, self.root)
            except (OSError, ValueError, KeyError) as error:
                LOGGER.warning("Unable to import %s source archive: %s", provider, error)
                self.send_error(400, "Unable to import source archive")
                return
            finally:
                temporary.unlink(missing_ok=True)
            self.send_response(303)
            self.send_header("Location", f"/?{urlencode({'provider': provider, 'show_empty': 1})}")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return
        form = parse_qs(body.decode("utf-8", errors="replace"))
        provider = form.get("provider", ["copilot"])[0]
        session_id = form.get("session", [""])[0]
        show_empty = form.get("show_empty", ["0"])[0] in {"1", "true", "yes"}
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
        self.send_header("Location", f"/?{urlencode({'provider': provider, 'show_empty': int(show_empty)})}")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def do_GET(self) -> None:
        parsed_url = urlparse(self.path)
        if parsed_url.path == "/export":
            query = parse_qs(parsed_url.query)
            provider = query.get("provider", [""])[0]
            session_id = query.get("session", [""])[0]
            if provider not in PROVIDERS or not session_id:
                self.send_error(400, "Invalid export request")
                return
            summary = next((item for item in load_session_index(self.root, provider, True) if item["id"] == session_id), None)
            if summary is None:
                self.send_error(404, "Session not found")
                return
            descriptor, temporary_name = tempfile.mkstemp(prefix="session-export-", suffix=".zip")
            os.close(descriptor)
            temporary = Path(temporary_name)
            try:
                export_session_sources(summary, provider, temporary)
                body = temporary.read_bytes()
            except (OSError, ValueError) as error:
                LOGGER.warning("Unable to export %s source archive: %s", provider, error)
                self.send_error(500, "Unable to export source files")
                return
            finally:
                temporary.unlink(missing_ok=True)
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", f'attachment; filename="{provider}-{session_id}.zip"')
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        query = parse_qs(parsed_url.query)
        provider = query.get("provider", ["copilot"])[0]
        if provider not in PROVIDERS:
            provider = "copilot"
        selected = query.get("session", [None])[0]
        turn_value = query.get("turn", [None])[0]
        selected_turn = int(turn_value) if turn_value and turn_value.isdigit() else None
        selected_metric = query.get("metric", [None])[0]
        selected_raw = query.get("raw", ["0"])[0] in {"1", "true", "yes"}
        show_empty = query.get("show_empty", ["0"])[0] in {"1", "true", "yes"}
        try:
            body = render(self.root, selected, selected_turn, selected_metric, provider, show_empty, selected_raw).encode("utf-8")
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
    parser = argparse.ArgumentParser(description="View Copilot session token usage")
    parser.add_argument("--root", type=Path, default=github_copilot_provider.default_root(), help="Copilot/agent session root folder")
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


PAGE = r'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>__APP_NAME__</title><style>
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

PAGE = PAGE.replace('</head>', '<style>.turn header{display:flex;align-items:center;justify-content:space-between;gap:12px}.turn-kind{color:#9ed1ff;background:#1b4268;border:1px solid #326b9e;border-radius:999px;padding:3px 9px;font-size:10px;font-weight:700;letter-spacing:.4px;text-transform:uppercase;white-space:nowrap}.show-raw{display:block;margin:10px 16px 14px;padding:7px 12px;border:1px solid #315479;border-radius:6px;background:#142a43;color:#9ed1ff;text-align:center;text-decoration:none;font-size:11px}.show-raw:hover{background:#1b4268;color:#fff}.content-layout{align-items:stretch}.explorer{height:calc(100vh - 84px);max-height:calc(100vh - 84px);position:sticky;top:42px;overflow:hidden;display:flex;flex-direction:column}.explorer-header{display:flex;align-items:center;justify-content:space-between;gap:12px;flex:none;overflow:hidden;background:transparent;padding:10px 12px;border-bottom:1px solid #223753;z-index:1}.explorer-header h2{margin:0}.explorer-body{display:flex;flex:1;min-height:0;flex-direction:column;overflow-y:auto;overflow-x:hidden;padding-top:12px;scrollbar-width:thin;scrollbar-color:#416b98 #0c1627}.explorer-body>p{flex:none}.explorer-body>pre{flex:none;max-height:none;overflow:visible}.explorer details{display:block;flex:none}.explorer details pre{max-height:none;overflow:visible}@media(max-width:700px){.explorer{height:auto;max-height:none;position:static;overflow:visible}.explorer-header{position:static;border-bottom:0}.explorer-body{display:block;overflow:visible}.explorer-body>pre{max-height:60vh;overflow:auto}.explorer details{display:block}.explorer details pre{max-height:60vh;overflow:auto}}</style></head>')

PAGE = PAGE.replace('</head>', '<style>.turn-invocations{margin:12px 16px 4px;padding:10px 12px;border:1px solid #263e5d;border-radius:8px;background:#101f34}.invocations-label{margin-bottom:7px;color:#89a6c7;font-size:10px;font-weight:700;letter-spacing:.8px;text-transform:uppercase}.turn-invocation{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:7px 0;border-top:1px solid #203653}.turn-invocation>span{color:#c8dbf2;font-size:11px;white-space:nowrap}.invocation-metrics{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:5px;flex:1}.invocation-metrics .metric{padding:5px 6px}.invocation-metrics .metric span{font-size:8px}.invocation-metrics .metric strong{font-size:11px}.invocation-count{margin-left:auto;color:#9ed1ff;background:#1b4268;border:1px solid #326b9e;border-radius:999px;padding:3px 9px;font-size:10px;font-weight:700;white-space:nowrap}@media(max-width:700px){.turn-invocation{display:block}.invocation-metrics{margin-top:6px}.turn-invocation .invocation-metrics{grid-template-columns:repeat(2,minmax(0,1fr))}}</style></head>')

PAGE = PAGE.replace('</body></html>', r'''<script>
(function(){
    var area=document.querySelector('.sessions-area');
    if(!area) return;
    var params=new URLSearchParams(window.location.search);
    var key='session-sidebar-scroll:'+ (params.get('provider') || 'copilot') + ':' + (params.get('show_empty') || '0');
    var saved=sessionStorage.getItem(key);
    if(saved !== null) area.scrollTop=Number(saved);
    function remember(){sessionStorage.setItem(key,String(area.scrollTop));}
    area.addEventListener('scroll',remember,{passive:true});
    document.querySelectorAll('.session-link,.provider,.refresh-sessions,.empty-toggle').forEach(function(link){
        link.addEventListener('click',remember);
    });
})();
</script><script>(function(){var params=new URLSearchParams(window.location.search);var key='session-detail-scroll:'+ (params.get('provider') || 'copilot') + ':' + (params.get('session') || '');var detail=document.querySelector('.detail');var saved=sessionStorage.getItem(key);if(saved !== null){requestAnimationFrame(function(){try{var position=JSON.parse(saved);if(detail){detail.scrollTop=Number(position.detail || 0);}window.scrollTo(0,Number(position.window || 0));}catch(error){if(detail){detail.scrollTop=Number(saved);}}});}document.querySelectorAll('.metric.clickable,.show-raw').forEach(function(link){link.addEventListener('click',function(){var current=document.querySelector('.detail');sessionStorage.setItem(key,JSON.stringify({detail:current ? current.scrollTop : 0,window:window.scrollY}));});});})();</script><script>document.querySelectorAll('form[action="/delete"]').forEach(function(form){form.addEventListener('submit',function(event){if(!event.defaultPrevented){return;}var overlay=document.querySelector('.loading');if(overlay){overlay.remove();}document.body.classList.remove('is-loading');var button=form.querySelector('button');if(button){button.disabled=false;}});});</script></body></html>''')

PAGE = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="color-scheme" content="dark">
<title>__APP_NAME__</title>
<style>
:root{
    --bg:#080b12;--surface:#0e131d;--surface-2:#131a27;--surface-3:#182233;
    --line:#202a3a;--line-strong:#2d3a50;--text:#f1f5fb;--muted:#8c9bb0;
    --subtle:#65748a;--accent:#7c8cff;--accent-2:#57d4b2;--blue:#63b3ff;
    --danger:#ff7285;--sidebar:330px;--radius:14px;--shadow:0 18px 50px rgba(0,0,0,.28);
    font-family:Inter,"Segoe UI",system-ui,-apple-system,sans-serif;color:var(--text);background:var(--bg);
}
*{box-sizing:border-box}[hidden]{display:none!important}html{scroll-behavior:smooth}body{margin:0;min-width:320px;background:radial-gradient(circle at 72% -20%,rgba(70,83,170,.13),transparent 36%),var(--bg);color:var(--text)}
a,button,input{font:inherit}a{color:inherit}button{color:inherit}.app{min-height:100vh}.mobile-bar{display:none}.sidebar{position:fixed;inset:0 auto 0 0;width:var(--sidebar);display:flex;flex-direction:column;background:rgba(12,16,25,.96);border-right:1px solid var(--line);z-index:20;backdrop-filter:blur(18px)}
.sidebar-top{padding:24px 20px 16px}.brand-row{display:flex;align-items:center;gap:11px}.brand-mark{display:grid;place-items:center;width:34px;height:34px;border-radius:10px;background:linear-gradient(145deg,#8b94ff,#5666de);box-shadow:0 8px 24px rgba(104,117,242,.3);font-size:17px;font-weight:800}.brand{font-size:15px;font-weight:750;letter-spacing:-.01em}.brand-subtitle{margin-top:2px;color:var(--subtle);font-size:11px}.path{margin:15px 0 0;padding:9px 10px;border:1px solid var(--line);border-radius:9px;background:#090d15;color:#718198;font:10px/1.45 ui-monospace,SFMono-Regular,Consolas,monospace;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.import-inline{display:inline-flex;margin:0}.import-inline input[type=file]{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}.import-button,.session-export-button{display:inline-flex;align-items:center;justify-content:center;width:34px;height:28px;border:1px solid var(--line-strong);border-radius:7px;background:var(--surface-3);color:var(--text);padding:0;font-size:14px;cursor:pointer;text-decoration:none;white-space:nowrap}.session-export-button{height:34px}.import-button:hover,.session-export-button:hover{border-color:var(--accent);background:#202c4a}.empty-toggle svg,.import-button svg,.session-export-button svg{width:16px;height:16px;fill:none;stroke:currentColor;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}
.provider-menu{display:grid;gap:3px;margin-top:14px;padding:4px;border:1px solid var(--line);border-radius:11px;background:#090d15}.provider{display:flex;align-items:center;gap:10px;min-width:0;height:34px;padding:0 11px;border-radius:7px;color:#8291a6;text-decoration:none;font-size:11px;font-weight:600}.provider>span:last-child{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.provider-mark{width:8px;height:8px;flex:none;border-radius:50%;background:#68768a;transition:.2s}.provider[data-provider="copilot"] .provider-mark{background:#a995ff}.provider[data-provider="codex"] .provider-mark{background:#70d6b5}.provider[data-provider="claude"] .provider-mark{background:#e8a06c}.provider[data-provider="m365_copilot"] .provider-mark{background:#58a7ff}.provider:hover{background:var(--surface-3);color:#c7d0de}.provider.selected{background:#20283a;color:#f1f5fb;box-shadow:inset 0 0 0 1px #35425a}.provider.selected .provider-mark{transform:scale(1.18);box-shadow:0 0 0 3px rgba(124,140,255,.12)}
.sessions-area{display:flex;flex:1;min-height:0;flex-direction:column}.sessions-heading{padding:12px 20px 10px}.heading-line{display:flex;align-items:center;justify-content:space-between}.count{color:#aab5c6;font-size:10px;font-weight:700;letter-spacing:.12em;text-transform:uppercase}.count b{display:inline-grid;place-items:center;min-width:20px;height:20px;margin-left:6px;border-radius:6px;background:#1b2432;color:#cdd6e3;letter-spacing:0}.sessions-heading-actions{display:flex;gap:6px}.empty-toggle,.refresh-sessions{display:grid;place-items:center;height:28px;border:1px solid var(--line);border-radius:7px;background:var(--surface);color:var(--muted);text-decoration:none;font-size:10px}.empty-toggle{width:34px;padding:0;font-size:14px}.refresh-sessions{width:34px;font-size:15px}.empty-toggle:hover,.refresh-sessions:hover{border-color:var(--line-strong);background:var(--surface-3);color:var(--text)}
.search-wrap{position:relative;margin-top:10px}.search-wrap svg{position:absolute;left:10px;top:9px;width:14px;color:#66758a}.session-search{width:100%;height:34px;padding:0 30px 0 31px;border:1px solid var(--line);border-radius:9px;outline:none;background:#090d15;color:var(--text);font-size:11px}.session-search::placeholder{color:#58677a}.session-search:focus{border-color:#5968bd;box-shadow:0 0 0 3px rgba(89,104,189,.12)}.search-key{position:absolute;right:8px;top:8px;color:#56657a;font:10px ui-monospace,monospace}
.session-list{flex:1;min-height:0;overflow:auto;padding:0 10px 18px;scrollbar-width:thin;scrollbar-color:#344056 transparent}.empty-search{margin:24px 10px;padding:20px;border:1px dashed var(--line-strong);border-radius:10px;color:#6f7f95;text-align:center;font-size:11px}.session-row{position:relative;display:flex;align-items:center;margin:2px 0}.session{display:flex;align-items:flex-start;gap:10px;min-width:0;flex:1;padding:10px 34px 10px 10px;border:1px solid transparent;border-radius:11px;color:#b9c4d3;text-decoration:none}.session:hover{background:#131a26}.session.selected{border-color:#293750;background:linear-gradient(100deg,#182235,#131a27);color:#fff}.session-glyph{display:grid;place-items:center;width:27px;height:27px;flex:none;border:1px solid #29354a;border-radius:8px;background:#171f2e;color:#8396b2;font-size:10px;font-weight:800}.session.selected .session-glyph{border-color:#5362ad;background:#2a335b;color:#cdd3ff}.session-copy{min-width:0;flex:1}.session b{display:block;overflow:hidden;color:inherit;font-size:12px;font-weight:650;line-height:1.35;text-overflow:ellipsis;white-space:nowrap}.session small{display:block;overflow:hidden;margin-top:3px;color:#647389;font:9px/1.3 ui-monospace,monospace;text-overflow:ellipsis;white-space:nowrap}.session .session-meta{color:#73829a;font-family:inherit}.session-row form{position:absolute;right:8px;top:9px}.delete-session{display:grid;place-items:center;width:25px;height:25px;padding:0;border:0;border-radius:7px;background:transparent;color:#56657a;cursor:pointer;font-size:16px;opacity:0}.session-row:hover .delete-session,.delete-session:focus{opacity:1}.delete-session:hover{background:#331923;color:var(--danger)}
.detail{width:calc(100% - var(--sidebar));min-height:100vh;margin-left:var(--sidebar);padding:42px clamp(24px,4vw,64px) 90px}.detail-heading{display:flex;align-items:flex-start;justify-content:space-between;gap:24px;max-width:1500px;margin:auto}.heading-copy{min-width:0}.eyebrow{display:flex;align-items:center;gap:7px;color:#8c9bb0;font-size:10px;font-weight:750;letter-spacing:.13em;text-transform:uppercase}.eyebrow>span{width:6px;height:6px;border-radius:50%;background:var(--accent-2);box-shadow:0 0 0 4px rgba(87,212,178,.09)}h1{max-width:900px;margin:8px 0 11px;font-size:clamp(25px,3vw,39px);font-weight:720;letter-spacing:-.035em;line-height:1.08;overflow-wrap:anywhere}.header-chips{display:flex;flex-wrap:wrap;gap:6px}.header-chips span{padding:5px 8px;border:1px solid var(--line);border-radius:7px;background:rgba(18,24,35,.7);color:#8090a6;font-size:10px}.detail-actions{display:flex;gap:7px}.icon-button{display:grid;place-items:center;width:34px;height:34px;padding:0;border:1px solid var(--line-strong);border-radius:9px;background:var(--surface-2);color:#a9b5c7;text-decoration:none;cursor:pointer}.icon-button:hover{background:var(--surface-3);color:#fff}.icon-button.danger:hover{border-color:#713243;background:#351721;color:var(--danger)}.detail-refresh{width:34px;height:34px;padding:0}
.session-facts{display:grid;grid-template-columns:minmax(160px,.7fr) minmax(220px,1fr) minmax(220px,1.3fr);max-width:1500px;margin:27px auto 0;border:1px solid var(--line);border-radius:12px;background:rgba(13,18,27,.72);overflow:hidden}.session-facts>div{min-width:0;padding:11px 14px;border-right:1px solid var(--line)}.session-facts>div:last-child{border:0}.session-facts span{display:block;margin-bottom:4px;color:#59687d;font-size:9px;font-weight:750;letter-spacing:.08em;text-transform:uppercase}.session-facts code{display:block;overflow:hidden;color:#8595aa;font:10px ui-monospace,SFMono-Regular,Consolas,monospace;text-overflow:ellipsis;white-space:nowrap}.provider-note{max-width:1500px;margin:10px auto 0;padding:10px 13px;border:1px solid #39452e;border-radius:9px;background:#171d13;color:#aeb99e;font-size:11px}.provider-note b{margin-right:8px;color:#c9d3ba}
.overview,.conversation{max-width:1500px;margin:38px auto 0}.section-heading{display:flex;align-items:flex-end;justify-content:space-between;gap:20px;margin-bottom:13px}.section-kicker{display:block;margin-bottom:4px;color:#5f7088;font-size:9px;font-weight:800;letter-spacing:.16em}.section-heading h2,.explorer-header h2{margin:0;font-size:16px;font-weight:680;letter-spacing:-.01em}.overview-stats{display:flex;align-items:center;gap:8px}.overview-stats span{padding:6px 9px;border:1px solid var(--line);border-radius:8px;color:#6f7e92;font-size:10px}.overview-stats b{color:#cbd4e1;font-weight:700}.metrics{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:8px}.metric{position:relative;display:flex;min-width:0;min-height:70px;flex-direction:column;align-items:center;justify-content:center;padding:14px 15px;border:1px solid var(--line);border-radius:11px;background:linear-gradient(145deg,#121925,#0f151f);text-align:center;overflow:hidden}.metric:before{content:"";position:absolute;inset:auto 0 0;height:2px;background:#6676d8;opacity:.5}.metric[data-metric="cacheReadTokens"]:before{background:#42b8c4}.metric[data-metric="cacheWriteTokens"]:before{background:#b985e5}.metric[data-metric="outputTokens"]:before{background:#54c99f}.metric[data-metric="reasoningTokens"]:before{background:#e4a35f}.metric span{display:block;width:100%;overflow:hidden;color:#697a91;font-size:9px;font-weight:700;letter-spacing:.06em;text-overflow:ellipsis;text-transform:uppercase;white-space:nowrap}.metric strong{display:block;margin-top:8px;color:#e9eef7;font:600 19px/1 ui-monospace,SFMono-Regular,Consolas,monospace}.metric.compact{min-height:52px;padding:9px 10px;border-radius:8px;text-decoration:none}.metric.compact strong{margin-top:5px;font-size:12px}.metric.clickable{transition:transform .15s,border-color .15s,background .15s}.metric.clickable:hover{transform:translateY(-1px);border-color:#42516a;background:#182131}.metric.clickable.active{border-color:#6878db;background:#202843;box-shadow:0 0 0 2px rgba(104,120,219,.11)}
.muted{color:#687990;font-size:10px}.content-layout{display:grid;grid-template-columns:minmax(0,1fr) minmax(280px,340px);align-items:start;gap:14px}.turns{display:grid;gap:12px;min-width:0}.turn{min-width:0;border:1px solid var(--line);border-radius:var(--radius);background:rgba(14,19,29,.82);box-shadow:0 12px 36px rgba(0,0,0,.08);overflow:hidden}.turn>header{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:12px 14px;border-bottom:1px solid var(--line);background:#111722}.turn-number{display:flex;align-items:center;gap:10px}.turn-number>span{display:grid;place-items:center;width:29px;height:29px;border:1px solid #2c3950;border-radius:8px;background:#182131;color:#8191aa;font:10px ui-monospace,monospace}.turn-number b{display:block;font-size:12px}.turn-number small{display:block;margin-top:2px;color:#5f6e83;font-size:9px}.turn-badges{display:flex;gap:5px}.step-count,.turn-kind,.summary-count{padding:4px 7px;border:1px solid #34425a;border-radius:999px;background:#1a2332;color:#8fa0b8;font-size:8px;font-weight:700;letter-spacing:.04em;text-transform:uppercase}.message{display:grid;grid-template-columns:72px minmax(0,1fr);gap:10px;padding:14px 16px;border-bottom:1px solid rgba(32,42,58,.7)}.message.assistant{background:rgba(15,23,34,.6)}.role{display:flex;align-items:center;gap:7px;height:24px}.role>span{display:grid;place-items:center;width:22px;height:22px;border-radius:7px;background:#272d50;color:#bec6ff;font-size:8px;font-weight:800}.assistant .role>span{background:#15372f;color:#8ee4c9}.message label{color:#7f8ea4;font-size:9px;font-weight:750;text-transform:uppercase}.message p{min-width:0;margin:2px 0 0;color:#b9c5d5;font:11px/1.65 ui-monospace,SFMono-Regular,Consolas,monospace;overflow-wrap:anywhere;white-space:pre-wrap}.message.user p{color:#d8e0eb}.message.tool{display:block;background:#101722}.message.tool label{display:block;margin-bottom:4px}.message.tool code{display:inline-block;max-width:100%;margin-top:5px;color:#8ba0ba;white-space:pre-wrap;overflow-wrap:anywhere}
.turn-steps{margin:12px 14px;border:1px solid var(--line);border-radius:10px;background:#0b1018;overflow:hidden}.turn-steps>summary{display:flex;align-items:center;justify-content:space-between;padding:10px 12px;color:#7d8da4;font-size:9px;font-weight:750;letter-spacing:.08em;text-transform:uppercase;cursor:pointer;list-style:none}.turn-steps summary::-webkit-details-marker{display:none}.steps-list{padding:0 12px 6px}.turn-step{display:flex;align-items:flex-start;gap:12px;padding:10px 0;border-top:1px solid #1b2432}.step-name{display:flex;align-items:center;gap:7px;width:90px;flex:none;padding-top:8px;color:#8292a8;font-size:9px}.step-name i{width:5px;height:5px;border-radius:50%;background:#5869cb}.step-content{display:grid;min-width:0;flex:1;gap:8px}.step-tools{display:grid;grid-template-columns:minmax(0,1fr);gap:6px}.step-tool{width:100%;min-width:0;border:1px solid #263247;border-radius:7px;background:#111925;overflow:hidden}.step-tool>summary{display:flex;align-items:center;justify-content:space-between;gap:8px;padding:7px 9px;cursor:pointer;list-style:none}.tool-name{min-width:0;overflow:hidden;color:#aebcd0;font:9px ui-monospace,SFMono-Regular,Consolas,monospace;text-overflow:ellipsis;white-space:nowrap}.tool-status{padding:2px 5px;border-radius:999px;background:#252e3d;color:#8392a7;font-size:7px;font-weight:750;text-transform:uppercase}.tool-status.completed{background:#15352d;color:#7ed7bc}.tool-status.failed{background:#3a1c25;color:#ff91a0}.step-tool-body{padding:8px 9px;border-top:1px solid #263247;color:#75869d;font-size:9px}.tool-payload+ .tool-payload{margin-top:8px}.tool-payload b{display:block;margin-bottom:4px;color:#788ba4;font-size:8px;letter-spacing:.06em;text-transform:uppercase}.tool-payload pre{max-height:180px;margin:0;padding:7px;border-radius:6px;background:#090e16;color:#9cacbf;font:8px/1.5 ui-monospace,SFMono-Regular,Consolas,monospace;overflow:auto;white-space:pre-wrap;overflow-wrap:anywhere}.step-no-tools{padding:6px 8px;border:1px dashed #263247;border-radius:7px;color:#64758a;font-size:9px}.step-usage{display:grid;grid-template-columns:minmax(0,3fr) minmax(0,2fr);gap:6px}.step-metric-group{min-width:0;padding:7px;border:1px solid #202a3a;border-radius:8px;background:#0d131d}.step-group-title{display:flex;align-items:center;gap:6px;margin:0 2px 6px;color:#74849b;font-size:8px;font-weight:750;letter-spacing:.06em;text-transform:uppercase}.step-group-title span{display:grid;place-items:center;width:16px;height:16px;border-radius:5px;background:#272d50;color:#bec6ff;font-size:6px}.output-group .step-group-title span{background:#15372f;color:#8ee4c9}.step-metrics{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:5px;min-width:0}.output-group .step-metrics{grid-template-columns:repeat(2,minmax(0,1fr))}.step-metrics .metric{padding:6px 7px;background:#0f1621}.step-metrics .metric span{font-size:7px}.step-metrics .metric strong{margin-top:4px;font-size:9px}.turn-footer{padding:10px 14px 14px}.turn-metrics{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:6px}.show-raw{display:flex;align-items:center;justify-content:center;gap:8px;min-height:34px;margin-top:9px;padding:8px 12px;border:1px solid #2d3b53;border-radius:8px;background:#131b28;color:#a9b7ca;text-decoration:none;font-size:10px;font-weight:650;letter-spacing:.01em}.show-raw>span:first-child{color:#8292ff;font:9px ui-monospace,monospace}.show-raw .raw-arrow{margin-left:2px;color:#71829a;font:12px system-ui,sans-serif;transition:transform .15s}.show-raw:hover{border-color:#5262b1;background:#1c2540;color:#fff}.show-raw:hover .raw-arrow{transform:translateX(2px);color:#aeb7ff}
.turn-invocations{margin:12px 14px;border:1px solid var(--line);border-radius:10px;background:#0b1018;overflow:hidden}.turn-invocations>summary{display:flex;align-items:center;justify-content:space-between;padding:10px 12px;color:#7d8da4;font-size:9px;font-weight:750;letter-spacing:.08em;text-transform:uppercase;cursor:pointer;list-style:none}.turn-invocations summary::-webkit-details-marker{display:none}.invocations-list{padding:0 12px 6px}.turn-invocation{display:flex;align-items:flex-start;gap:12px;padding:10px 0;border-top:1px solid #1b2432}.invocation-name{display:flex;align-items:center;gap:7px;width:90px;flex:none;padding-top:8px;color:#8292a8;font-size:9px}.invocation-name i{width:5px;height:5px;border-radius:50%;background:#5869cb}.invocation-content{display:grid;min-width:0;flex:1;gap:8px}.invocation-tools{display:grid;grid-template-columns:minmax(0,1fr);gap:6px}.invocation-tool{width:100%;min-width:0;border:1px solid #263247;border-radius:7px;background:#111925;overflow:hidden}.invocation-tool>summary{display:flex;align-items:center;justify-content:space-between;gap:8px;padding:7px 9px;cursor:pointer;list-style:none}.invocation-tool-body{padding:8px 9px;border-top:1px solid #263247;color:#75869d;font-size:9px}.invocation-no-tools{padding:6px 8px;border:1px dashed #263247;border-radius:7px;color:#64758a;font-size:9px}.invocation-usage{display:grid;grid-template-columns:minmax(0,3fr) minmax(0,2fr);gap:6px}.invocation-metric-group{min-width:0;padding:7px;border:1px solid #202a3a;border-radius:8px;background:#0d131d}.invocation-group-title{display:flex;align-items:center;gap:6px;margin:0 2px 6px;color:#74849b;font-size:8px;font-weight:750;letter-spacing:.06em;text-transform:uppercase}.invocation-group-title span{display:grid;place-items:center;width:16px;height:16px;border-radius:5px;background:#272d50;color:#bec6ff;font-size:6px}.output-group .invocation-group-title span{background:#15372f;color:#8ee4c9}.invocation-metrics{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:5px;min-width:0}.output-group .invocation-metrics{grid-template-columns:repeat(2,minmax(0,1fr))}.invocation-metrics .metric{padding:6px 7px;background:#0f1621}.invocation-metrics .metric span{font-size:7px}.invocation-metrics .metric strong{margin-top:4px;font-size:9px}.invocation-count{padding:4px 7px;border:1px solid #34425a;border-radius:999px;background:#1a2332;color:#8fa0b8;font-size:8px;font-weight:700;letter-spacing:.04em;text-transform:uppercase}.explorer{position:sticky;top:20px;display:flex;min-width:0;max-height:calc(100vh - 40px);flex-direction:column;border:1px solid var(--line);border-radius:var(--radius);background:#0c111a;box-shadow:var(--shadow);overflow:hidden}.explorer-header{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:14px;border-bottom:1px solid var(--line);background:#111722}.explorer-close{display:grid;place-items:center;width:25px;height:25px;border-radius:7px;color:#617086;text-decoration:none;font-size:16px}.explorer-close:hover{background:#20293a;color:#fff}.explorer-body{min-height:170px;padding:14px;overflow:auto;scrollbar-width:thin;scrollbar-color:#344056 transparent}.explorer-body p,.explorer-body pre{margin:0;color:#9eacbf;font:10px/1.65 ui-monospace,SFMono-Regular,Consolas,monospace;overflow-wrap:anywhere;white-space:pre-wrap}.explorer-body pre{color:#aab8cb}.empty{display:flex;flex-direction:column;align-items:center;padding:50px;border:1px dashed var(--line-strong);border-radius:var(--radius);color:#6f8098;text-align:center}.empty b{color:#b6c1d1}.empty span{margin-top:5px;font-size:11px}.no-sessions{display:grid;place-items:center}.empty-hero{max-width:500px;text-align:center}.hero-icon{display:grid;place-items:center;width:54px;height:54px;margin:0 auto 18px;border:1px solid #303c52;border-radius:17px;background:linear-gradient(145deg,#192235,#101620);color:#94a3ff;font-size:23px;box-shadow:var(--shadow)}.empty-hero h1{margin:7px auto 10px}.empty-hero p{margin:0;color:#7989a0;font-size:13px;line-height:1.6}
.sidebar-scrim{display:none}.loading{position:fixed;inset:0;display:grid;place-items:center;background:rgba(5,8,13,.55);z-index:100;backdrop-filter:blur(3px)}.loading-card{display:flex;align-items:center;gap:10px;padding:12px 16px;border:1px solid #303b50;border-radius:11px;background:#111722;box-shadow:var(--shadow);color:#b6c1d2;font-size:11px}.spinner{width:17px;height:17px;border:2px solid #39445a;border-top-color:#8b98ff;border-radius:50%;animation:spin .7s linear infinite}@keyframes spin{to{transform:rotate(360deg)}}
:focus-visible{outline:2px solid #8190ff;outline-offset:2px}@media(max-width:1180px){:root{--sidebar:292px}.content-layout{grid-template-columns:minmax(0,1fr) 290px}.session-facts{grid-template-columns:1fr 1fr}.session-facts>div:last-child{grid-column:1/-1;border-top:1px solid var(--line)}}
@media(max-width:900px){.mobile-bar{position:sticky;top:0;z-index:15;display:flex;align-items:center;justify-content:space-between;height:54px;padding:0 14px;border-bottom:1px solid var(--line);background:rgba(8,11,18,.92);backdrop-filter:blur(16px)}.mobile-bar b{font-size:13px}.menu-button{width:34px;height:34px;border:1px solid var(--line);border-radius:9px;background:var(--surface);cursor:pointer}.sidebar{width:min(var(--sidebar),calc(100vw - 42px));transform:translateX(-102%);transition:transform .22s ease;box-shadow:var(--shadow)}body.nav-open .sidebar{transform:none}.sidebar-scrim{position:fixed;inset:0;z-index:19;background:rgba(3,5,9,.62)}body.nav-open .sidebar-scrim{display:block}.detail{width:100%;margin:0;padding:30px 20px 70px}.content-layout{grid-template-columns:1fr}.explorer{position:static;display:none;max-height:none;order:-1}.explorer.is-active{display:flex}.explorer-body{max-height:55vh}.session-facts{grid-template-columns:1fr}.session-facts>div,.session-facts>div:last-child{grid-column:auto;border:0;border-bottom:1px solid var(--line)}.session-facts>div:last-child{border:0}}
@media(max-width:620px){.detail{padding:24px 13px 60px}.detail-heading{align-items:flex-start}.detail-actions{flex-direction:column}.header-chips span:last-child{display:none}.overview-stats{display:none}.metrics,.turn-metrics{grid-template-columns:repeat(2,minmax(0,1fr))}.metrics .metric:last-child,.turn-metrics .metric:last-child{grid-column:1/-1}.metric{padding:11px}.metric strong{font-size:16px}.section-heading{align-items:flex-start}.section-heading>.muted{max-width:130px;text-align:right}.message{display:block;padding:13px}.role{margin-bottom:8px}.turn>header{align-items:flex-start}.turn-badges{flex-direction:column;align-items:flex-end}.turn-invocation{display:block}.invocation-name{width:auto;margin-bottom:7px}.invocation-usage{grid-template-columns:1fr}.invocation-metrics{grid-template-columns:repeat(3,minmax(0,1fr))}.output-group .invocation-metrics{grid-template-columns:repeat(2,minmax(0,1fr))}.turn-footer{padding:2px 10px 10px}.session-facts{margin-top:20px}h1{font-size:25px}}
@media(prefers-reduced-motion:reduce){*{scroll-behavior:auto!important;animation-duration:.01ms!important;transition-duration:.01ms!important}}
</style>
</head>
<body>
<div class="mobile-bar"><button class="menu-button" type="button" aria-label="Open sessions" aria-controls="sidebar">☰</button><b>__APP_NAME__</b><span style="width:34px"></span></div>
<div class="app">
    <aside class="sidebar" id="sidebar">
        <div class="sidebar-top">
            <div class="brand-row"><span class="brand-mark">AI</span><div><div class="brand">__APP_NAME__</div><div class="brand-subtitle">Local AI activity</div></div></div>
            <nav class="provider-menu" aria-label="AI providers">__PROVIDER_MENU__</nav>
        </div>
        <div class="sessions-area">
            <div class="sessions-heading">
                <div class="heading-line"><div class="count">Sessions <b>__SESSION_COUNT__</b></div><div class="sessions-heading-actions">__EMPTY_TOGGLE__<a class="refresh-sessions" href="__REFRESH_URL__" aria-label="Refresh sessions" title="Refresh sessions">↻</a>__IMPORT_FORM__</div></div>
                <div class="search-wrap"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="7"></circle><path d="m20 20-4-4"></path></svg><input class="session-search" type="search" placeholder="Filter sessions…" aria-label="Filter sessions"><span class="search-key">/</span></div>
            </div>
            <div class="session-list">__SESSION_ROWS__<div class="empty-search" hidden>No matching sessions</div></div>
        </div>
    </aside>
    <button class="sidebar-scrim" type="button" aria-label="Close sessions"></button>
    __DETAIL__
</div>
<script>
(function(){
    var body=document.body, sidebar=document.querySelector('.sidebar'), search=document.querySelector('.session-search');
    function closeNav(){body.classList.remove('nav-open')}
    document.querySelector('.menu-button').addEventListener('click',function(){body.classList.toggle('nav-open')});
    document.querySelector('.sidebar-scrim').addEventListener('click',closeNav);
    document.addEventListener('keydown',function(event){
        if(event.key==='Escape') closeNav();
        if(event.key==='/' && document.activeElement!==search){event.preventDefault();search.focus();}
    });
    search.addEventListener('input',function(){
        var query=search.value.trim().toLowerCase(), visible=0;
        document.querySelectorAll('.session-row').forEach(function(row){var show=!query||row.textContent.toLowerCase().includes(query);row.hidden=!show;if(show)visible++;});
        document.querySelector('.empty-search').hidden=visible!==0;
    });
    var params=new URLSearchParams(location.search), scrollKey='session-list:'+ (params.get('provider')||'copilot')+':'+(params.get('show_empty')||'0');
    var list=document.querySelector('.session-list'), saved=sessionStorage.getItem(scrollKey);if(saved)list.scrollTop=Number(saved);
    list.addEventListener('scroll',function(){sessionStorage.setItem(scrollKey,String(list.scrollTop))},{passive:true});
    var detailKey='session-detail:'+(params.get('session')||'');var position=sessionStorage.getItem(detailKey);if(position)requestAnimationFrame(function(){scrollTo(0,Number(position))});
    function loading(text){if(document.querySelector('.loading'))return;var layer=document.createElement('div');layer.className='loading';layer.innerHTML='<div class="loading-card"><span class="spinner"></span><span>'+text+'</span></div>';body.appendChild(layer);}
    document.querySelectorAll('a.session-link,a.provider,a.clickable,a.refresh-sessions,a.empty-toggle').forEach(function(link){link.addEventListener('click',function(){sessionStorage.setItem(detailKey,String(scrollY));loading('Loading session data…');});});
    document.querySelectorAll('form[action="/delete"]').forEach(function(form){form.addEventListener('submit',function(event){if(event.defaultPrevented)return;loading('Deleting conversation…');var button=form.querySelector('button');if(button)button.disabled=true;});});
    if(sidebar){sidebar.addEventListener('click',function(event){if(event.target.closest('.session-link')&&innerWidth<=900)closeNav();});}
})();
</script>
</body>
</html>'''

PAGE = PAGE.replace('</style>\n</head>', '''<style>
/* Compact invocation cards: tools stay vertical, metrics stay horizontal. */
.turn-invocations{margin:12px 14px;padding:0;border:1px solid #263247;border-radius:11px;background:#0b1018;overflow:hidden}
.turn-invocations>summary{padding:11px 13px;background:#111925}
.invocations-list{display:grid;gap:8px;padding:8px}
.turn-invocation{display:grid;grid-template-columns:92px minmax(0,1fr);align-items:start;gap:10px;padding:10px;border:1px solid #263247;border-radius:9px;background:#101722}
.invocation-name{width:auto;padding-top:5px;color:#91a2b9;font-size:9px;font-weight:700;letter-spacing:.03em}
.invocation-content{gap:8px}
.invocation-tools{gap:5px}
.invocation-tool{border-color:#2b3850;background:#131d2b;box-shadow:0 2px 8px rgba(0,0,0,.12)}
.invocation-tool>summary{min-height:31px;padding:7px 10px}
.invocation-tool-body{background:#0d141f}
.invocation-usage{grid-template-columns:minmax(0,3fr) minmax(0,2fr);gap:7px}
.invocation-metric-group{padding:7px;background:#0d141f}
.invocation-metrics{grid-template-columns:repeat(3,minmax(0,1fr))}
.output-group .invocation-metrics{grid-template-columns:repeat(2,minmax(0,1fr))}
@media(max-width:620px){.turn-invocation{display:block}.invocation-name{margin-bottom:7px}.invocation-usage{grid-template-columns:1fr}}
</style></head>''')


if __name__ == "__main__":
    main()
