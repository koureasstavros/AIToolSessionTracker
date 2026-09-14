"""GitHub Copilot-specific OpenTelemetry routing and session metadata."""
from __future__ import annotations

import json

PROVIDER = "copilot"


def is_internal_prompt(attributes: dict[str, object]) -> bool:
    value = str(attributes.get("copilot_chat.user_request") or attributes.get("gen_ai.input.messages") or "").lower()
    return "generate exactly 10 unique progress messages" in value


def matches(attributes: dict[str, object]) -> bool:
    value = str(
        attributes.get("ai.session.provider")
        or attributes.get("gen_ai.system")
        or attributes.get("gen_ai.provider.name")
        or ""
    ).lower()
    return value in {"github", "github_copilot", "copilot"} or str(attributes.get("service.name") or "") == "copilot-chat"


def session_id(attributes: dict[str, object], trace_id: str) -> str:
    return str(
        attributes.get("ai.session.id")
        or attributes.get("gen_ai.conversation.id")
        or attributes.get("session.id")
        or trace_id
    )


def request_title(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        return ""
    text = value
    try:
        payload = json.loads(value)
        if isinstance(payload, list):
            text = next(
                (str(item.get("text") or "") for item in payload if isinstance(item, dict) and item.get("text")),
                value,
            )
    except json.JSONDecodeError:
        pass
    if "<userRequest>" in text:
        text = text.split("<userRequest>", 1)[1].split("</userRequest>", 1)[0]
    return " ".join(text.strip().split())[:140]


def session_metadata(attribute_sets: list[dict[str, object]], fallback_id: str) -> tuple[str, str | None]:
    name = ""
    project = None
    for attributes in attribute_sets:
        if is_internal_prompt(attributes):
            continue
        if project is None:
            for key in ("copilot_chat.repo.remote_url", "github.copilot.git.repository", "git.repository"):
                value = attributes.get(key)
                if isinstance(value, str) and value.strip():
                    project = value.strip()
                    break
        if not name:
            candidate = request_title(attributes.get("copilot_chat.user_request"))
            if candidate.strip(' "\'') not in {"", "closing ="}:
                name = candidate
    return name or fallback_id, project


def handle_span(ctx, name: str, start_ns: int, end_ns: int, attributes: dict[str, object], raw_json: str) -> bool:
    """Handle spans that aren't chat turns: embeddings, progress helper calls,
    and the standalone ``execute_tool`` span carrying a tool call's result.

    Returns True when this span was fully handled and should not also be
    turned into a generic chat turn.
    """
    if str(name).lower().startswith("embeddings"):
        return True
    if is_internal_prompt(attributes):
        ctx.internal_spans.append((name, start_ns, end_ns, attributes, raw_json))
        return True
    if str(attributes.get("gen_ai.operation.name") or "") == "execute_tool":
        # The tool call itself is requested inside a chat span's output
        # messages; the result lands on this separate span, tied together by
        # the same tool call id. Fill in the result on the existing record
        # instead of adding a contentless turn for this span.
        call_id = str(attributes.get("gen_ai.tool.call.id") or "")
        tool = ctx.pending_tools.get(call_id)
        if tool is not None:
            result = attributes.get("gen_ai.tool.call.result")
            if result is not None:
                tool["result"] = result
                tool["status"] = "completed"
            if not tool.get("arguments"):
                arguments = ctx.tool_arguments(attributes.get("gen_ai.tool.call.arguments"))
                if arguments is not None:
                    tool["arguments"] = arguments
        return True
    return False


def split_user_request(text: str) -> tuple[str, list[dict[str, str]]]:
    """Keep Copilot's prompt context separate from the actual user request."""
    if "closing =" in text:
        prefix, suffix = text.split("closing =", 1)
        prefix = prefix.strip().strip('"#').strip()
        context = suffix.strip()
        if prefix:
            return prefix, [{"name": "Additional context", "content": context}] if context else []
        return "", [{"name": "Additional context", "content": context}] if context else []
    opening = "<userRequest>"
    closing = "</userRequest>"
    if opening not in text or closing not in text:
        return text, []
    start = text.index(opening) + len(opening)
    position = start
    depth = 1
    end = -1
    while depth:
        next_open = text.find(opening, position)
        next_close = text.find(closing, position)
        if next_close < 0:
            request = text[start:]
            nested_context = request.find("<context>")
            if nested_context >= 0:
                leading = request[:nested_context].strip()
                request_lines = [line for line in leading.splitlines() if line.strip()]
                current_request = request_lines[0] if request_lines else ""
                quoted_prefix = "\n".join(request_lines[1:])
                context = (text[:start - len(opening)] + quoted_prefix + request[nested_context:]).strip()
                return current_request, [{"name": "Additional context", "content": context}]
            return request.strip(), [{"name": "Additional context", "content": text[:start - len(opening)].strip()}]
        if 0 <= next_open < next_close:
            depth += 1
            position = next_open + len(opening)
            continue
        depth -= 1
        if depth == 0:
            end = next_close
            break
        position = next_close + len(closing)
    request = text[start:end]
    # A user can paste an earlier prompt envelope inside a new request. Keep
    # their leading instruction as the user message and treat the pasted
    # envelope as additional context rather than displaying it as their text.
    nested_context = request.find("<context>")
    nested = ""
    if nested_context >= 0:
        nested = request[nested_context:]
        request = request[:nested_context]
    context = (text[:start - len(opening)] + nested + text[end + len(closing):]).strip()
    instructions = [{"name": "Additional context", "content": context}] if context else []
    return request.strip(), instructions


def delegated_agent_from_tool(ctx, tool: dict) -> dict | None:
    """Build a lightweight delegated-agent record from a subagent tool call."""
    if str(tool.get("name") or "").lower() not in {"runsubagent", "run_subagent", "task"}:
        return None
    arguments = tool.get("arguments")
    arguments = arguments if isinstance(arguments, dict) else {}
    name = arguments.get("agentName") or arguments.get("name") or "Delegated agent"
    description = arguments.get("description") or arguments.get("agentDescription") or name
    return {
        "id": str(tool.get("id") or name), "name": str(name), "agentDescription": str(description),
        "tokens": ctx.empty_tokens(), "turns": [],
    }
