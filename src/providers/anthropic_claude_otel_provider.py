"""Anthropic Claude-specific OpenTelemetry routing and session metadata."""
from __future__ import annotations

PROVIDER = "claude"


def matches(attributes: dict[str, object]) -> bool:
    value = str(attributes.get("ai.session.provider") or attributes.get("gen_ai.system") or attributes.get("gen_ai.provider.name") or "").lower()
    service = str(attributes.get("service.name") or "").lower()
    return value in {"anthropic", "claude", "claude_code"} or service in {"claude-code", "claude-code-desktop"}


def session_id(attributes: dict[str, object], trace_id: str) -> str:
    return str(attributes.get("ai.session.id") or attributes.get("gen_ai.conversation.id") or attributes.get("session.id") or trace_id)


def session_metadata(attribute_sets: list[dict[str, object]], fallback_id: str) -> tuple[str, str | None]:
    name = next((str(attributes["prompt"]).strip()[:140] for attributes in attribute_sets if attributes.get("event.name") in {"claude_code.user_prompt", "user_prompt"} and str(attributes.get("prompt") or "").strip()), fallback_id)
    project = next((str(attributes[key]) for attributes in attribute_sets for key in ("vcs.repository.url.full", "code.workspace.folder", "workspace.path", "git.repository") if isinstance(attributes.get(key), str) and attributes[key].strip()), None)
    return name, project


def handle_span(ctx, name: str, start_ns: int, end_ns: int, attributes: dict[str, object], raw_json: str) -> bool:
    """Skip claude_code.* spans (llm_request/tool/tool.execution/interaction).

    Their chat content and token usage live entirely in otel_logs
    (user_prompt/assistant_response/api_request); these spans carry no
    message content and repeat the same per-request token counts, so
    building turns from them here would add empty turns and double-count
    usage already read from the logs. The model name is still useful, so
    stash it for the caller to pick up after this span is skipped.
    """
    if "model" not in ctx.extra:
        model = attributes.get("gen_ai.request.model") or attributes.get("model")
        if model:
            ctx.extra["model"] = model
    return True


def is_user_prompt_event(event_name: str) -> bool:
    return event_name in {"claude_code.user_prompt", "user_prompt"}


def handle_log_event(ctx, event_name: str, attributes: dict[str, object], raw_json: str, log_turn: dict, invocation: dict) -> bool:
    if event_name in {"claude_code.assistant_response", "assistant_response"}:
        response = str(attributes.get("response") or "").strip()
        if response:
            log_turn["assistant"].append(response)
            invocation["assistant"].append(response)
        return True
    if event_name in {"claude_code.tool_result", "tool_result", "claude_code.tool_decision", "tool_decision"}:
        tool_parameters = ctx.tool_arguments(attributes.get("tool_parameters"))
        tool_parameters = tool_parameters if isinstance(tool_parameters, dict) else {}
        tool_input = ctx.tool_arguments(attributes.get("tool_input"))
        tool_input = tool_input if isinstance(tool_input, dict) else {}
        tool_name = str(attributes.get("tool_name") or attributes.get("tool_name_safe") or "tool")
        tool = {
            "id": str(attributes.get("tool_use_id") or attributes.get("gen_ai.tool.call.id") or ""),
            "name": tool_name,
            "status": "completed" if event_name.endswith("tool_result") else str(attributes.get("decision") or "recorded"),
            "result": attributes.get("tool_input") or attributes.get("tool_parameters"),
        }
        # The ``Agent``/``Task`` tool delegates to a subagent, but its type
        # and prompt only appear inside the JSON-encoded tool_parameters /
        # tool_input fields, not as top-level attributes.
        subagent_type = tool_parameters.get("subagent_type") or attributes.get("subagent_type")
        if subagent_type or attributes.get("agent_id") or tool_name.lower() in {"agent", "task"}:
            subagent = {
                "id": str(attributes.get("agent_id") or tool["id"]),
                "name": str(subagent_type or "Delegated agent"),
                "agentDescription": str(tool_input.get("description") or tool_input.get("prompt") or subagent_type or "Delegated agent"),
                "tokens": ctx.empty_tokens(), "turns": [],
            }
            tool["subagent"] = subagent
            prompt_key = str(attributes.get("prompt.id") or "")
            if prompt_key:
                ctx.pending_subagents[prompt_key] = subagent
        invocation["tools"].append(tool)
        log_turn["tools"].append(tool)
        return True
    if event_name == "subagent_completed":
        # Claude Code never emits the subagent's own model calls as
        # separately correlatable turns, only this aggregate summary once
        # the delegated run finishes. Fill in what it reports instead of
        # leaving the subagent's tokens permanently unknown.
        subagent = ctx.pending_subagents.get(str(attributes.get("prompt.id") or ""))
        if subagent is not None:
            total_tokens = attributes.get("total_tokens")
            try:
                subagent["totalTokens"] = int(total_tokens) if total_tokens is not None else None
            except (TypeError, ValueError):
                subagent["totalTokens"] = None
            subagent["toolUses"] = attributes.get("total_tool_uses")
            subagent["durationMs"] = attributes.get("duration_ms")
            subagent["model"] = attributes.get("final_model") or attributes.get("model")
        return True
    if event_name in {"claude_code.api_request", "api_request"}:
        values = ctx.token_values(attributes)
        invocation["tokens"] = values
        log_turn["tokens"] = values
        invocation["tokenFields"] = [key for key, value in values.items() if value is not None]
        log_turn["tokenFields"] = list(invocation["tokenFields"])
        return True
    return False
