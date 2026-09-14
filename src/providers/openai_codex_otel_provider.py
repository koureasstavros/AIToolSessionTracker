"""OpenAI Codex-specific OpenTelemetry routing and session metadata."""
from __future__ import annotations

import json

PROVIDER = "codex"

_USAGE_ATTRIBUTE_KEYS = (
    "gen_ai.usage.input_tokens", "gen_ai.usage.output_tokens",
    "gen_ai.usage.cache_read.input_tokens", "gen_ai.usage.cache_write.input_tokens",
    "codex.usage.reasoning_output_tokens", "codex.usage.total_tokens",
)


def is_internal_prompt(attributes: dict[str, object]) -> bool:
    prompt = str(attributes.get("prompt") or "").lower()
    return attributes.get("event.name") == "codex.user_prompt" and "short title for a task" in prompt


def matches(attributes: dict[str, object]) -> bool:
    value = str(
        attributes.get("ai.session.provider")
        or attributes.get("gen_ai.system")
        or attributes.get("gen_ai.provider.name")
        or ""
    ).lower()
    return value in {"openai", "codex"} or "codex" in str(attributes.get("service.name") or "").lower()


def session_id(attributes: dict[str, object], trace_id: str) -> str:
    return str(
        attributes.get("ai.session.id")
        or attributes.get("gen_ai.conversation.id")
        or attributes.get("conversation.id")
        or attributes.get("session.id")
        or trace_id
    )


def session_metadata(attribute_sets: list[dict[str, object]], fallback_id: str) -> tuple[str, str | None]:
    name = next(
        (str(attributes.get("prompt")).strip()[:140] for attributes in attribute_sets
         if attributes.get("event.name") == "codex.user_prompt" and not is_internal_prompt(attributes) and str(attributes.get("prompt") or "").strip()),
        fallback_id,
    )
    project = next(
        (str(attributes[key]) for attributes in attribute_sets for key in ("code.workspace.folder", "workspace.path", "git.repository")
         if isinstance(attributes.get(key), str) and attributes[key].strip()),
        None,
    )
    return name, project


def filter_span_rows(rows: list[tuple], related_trace_ids: set[str]) -> list[tuple]:
    """Keep only the spans that carry real model usage for this session.

    Codex's app-server process emits one shared OTLP trace for a whole
    process, including its own internal bookkeeping and every agent spawned
    within it (see ``codex.tool_result``/spawn_agent in ``handle_log_event``
    below) — thousands of unrelated spans share the session's trace id. Only
    the ``handle_responses`` span with actual token usage attributes is safe
    to fold into a turn here.
    """
    if not related_trace_ids:
        return rows
    filtered = []
    for row in rows:
        if row[0] != "handle_responses":
            continue
        try:
            attributes = json.loads(row[3])
        except json.JSONDecodeError:
            continue
        if any(key in attributes for key in _USAGE_ATTRIBUTE_KEYS):
            filtered.append(row)
    return filtered


def is_user_prompt_event(event_name: str) -> bool:
    return event_name == "codex.user_prompt"


def precompute_logs(ctx, log_rows: list[tuple]) -> None:
    """Index each spawned agent's own prompt and final answer.

    Codex spawns delegated agents via a ``spawn_agent`` tool call and joins
    them with ``wait_agent``. Both share one OTLP trace with the parent and
    every other spawned agent, so token usage spans can't be attributed to
    one agent — only the agent's own prompt (from its ``codex.user_prompt``
    log, keyed by conversation id) and its final answer (from
    ``wait_agent``'s completion status) are safe to surface without
    double-counting usage across siblings.
    """
    wait_agent_outcomes: dict[str, str] = {}
    child_prompts: dict[str, tuple[str, int]] = {}
    for _session_id, _trace_id, timestamp_ns, attributes_json, _raw_json in log_rows:
        try:
            attributes = json.loads(attributes_json)
        except json.JSONDecodeError:
            continue
        event_name = attributes.get("event.name")
        if event_name == "codex.tool_result" and str(attributes.get("tool_name") or "").lower() == "wait_agent":
            output = ctx.tool_arguments(attributes.get("output"))
            status = output.get("status") if isinstance(output, dict) else None
            if isinstance(status, dict):
                for agent_id, info in status.items():
                    if isinstance(info, dict) and info.get("completed"):
                        wait_agent_outcomes[str(agent_id)] = str(info["completed"])
        elif event_name == "codex.user_prompt":
            conversation_id = str(attributes.get("conversation.id") or "")
            prompt_text = str(attributes.get("prompt") or "").strip()
            if conversation_id and prompt_text:
                child_prompts.setdefault(conversation_id, (prompt_text, timestamp_ns))
    ctx.extra["wait_agent_outcomes"] = wait_agent_outcomes
    ctx.extra["child_prompts"] = child_prompts


def _subagent(ctx, agent_id: str, nickname: str, spawn_message: object) -> dict:
    prompt_text, prompt_timestamp_ns = ctx.extra.get("child_prompts", {}).get(agent_id, (None, 0))
    user_text = prompt_text or (str(spawn_message).strip() if spawn_message else "")
    assistant_text = ctx.extra.get("wait_agent_outcomes", {}).get(agent_id)
    child_invocation = {
        "index": 1, "assistant": [assistant_text] if assistant_text else [],
        "tokens": ctx.empty_tokens(), "tokenFields": [], "tools": [],
    }
    child_turn = {
        "id": f"sub-{agent_id}", "turn_index": 1,
        "timestamp": (prompt_timestamp_ns / 1_000_000_000) if prompt_timestamp_ns else None,
        "user": user_text, "assistant": [assistant_text] if assistant_text else [], "tools": [],
        "raw": [], "tokens": ctx.empty_tokens(), "tokenFields": [],
        "invocations": [child_invocation], "internalInstructions": [], "outputTokensExcludeReasoning": True,
    }
    return {
        "id": agent_id, "name": nickname, "agentDescription": user_text or nickname,
        "tokens": ctx.empty_tokens(), "turns": [child_turn],
    }


def handle_log_event(ctx, event_name: str, attributes: dict[str, object], raw_json: str, log_turn: dict, invocation: dict) -> bool:
    if event_name != "codex.tool_result":
        return False
    arguments = ctx.tool_arguments(attributes.get("arguments"))
    output = ctx.tool_arguments(attributes.get("output"))
    tool_name = str(attributes.get("tool_name") or "tool")
    tool = {
        "id": str(attributes.get("call_id") or ""), "name": tool_name, "arguments": arguments,
        "status": "completed" if attributes.get("success") else "failed", "result": output,
    }
    if tool_name.lower() == "spawn_agent" and isinstance(output, dict) and output.get("agent_id"):
        agent_id = str(output["agent_id"])
        nickname = str(output.get("nickname") or agent_id)
        spawn_message = arguments.get("message") if isinstance(arguments, dict) else None
        tool["subagent"] = _subagent(ctx, agent_id, nickname, spawn_message)
    invocation["tools"].append(tool)
    log_turn["tools"].append(tool)
    return True
