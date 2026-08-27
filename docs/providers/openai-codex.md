# OpenAI Codex provider parsing

## Sources

The provider scans JSONL rollout files under:

`%USERPROFILE%\\.codex\\sessions\\**\\*.jsonl`

The exact rollout path is retained as `_source` and displayed as the information source. The source label is OpenAI Codex and the tool surface is reported as CLI / VS Code integration.

The same local rollout storage can contain both Codex coding sessions and Codex
chat conversations created through the CLI or the VS Code integration. The
viewer reads both types from the JSONL transcript records; no separate chat
storage location is required.

## Actions

Codex exposes **Archive**, which functions as a delete operation for the local
rollout. The viewer exposes **Delete** and removes the selected JSONL rollout
file. This only removes the local transcript; it does not affect any remote
account data.

## Identity and metadata

- **GUID / ID:** Prefer `sessionId` or `session_id`. If the value contains a UUID, expose the UUID portion. Otherwise use the filename stem.
- **Name:** Prefer non-rollout values from `title`, `name`, `summary`, or `session_name`. If no title is persisted, derive a short name from the first real user message. Otherwise use the normalized session ID.
- **Datetime:** Use the rollout file modification time.
- **Model:** Prefer a model field found in the records. Fall back to `codex`.
- **Project:** Infer the project from recognized project/workspace records.
- **Source:** Use the exact rollout JSONL path.

## Turns

Records are grouped by `turn_id`, `turnId`, nested item turn ID, `promptId`, or a record UUID fallback. User and assistant messages are assigned from their roles and readable content.

Codex function calls are handled specially:

- Each `function_call` creates a separate turn marked `kind: tool`.
- The call's `call_id` pairs it with the matching `function_call_output`.
- Tool input displays the function/tool name and arguments.
- Tool output displays the returned result.
- Parallel calls remain separate tool turns.
- Raw records are retained on every turn.

Metadata-only records such as initial queue/attachment records are not displayed as turns.

## Tokens

Usage is read primarily from `event_msg` records containing `token_count.info.last_token_usage`, plus recognized usage aliases. Normalized fields are:

- `inputTokens`
- `cacheReadTokens`
- `cacheWriteTokens`
- `outputTokens`
- `reasoningTokens`

Totals are aggregated from parsed turns. Codex frequently stores usage for a model response or broader turn rather than for each individual function call. Tool turns without a matching usage record therefore legitimately have missing token values; duplicating one usage record across every tool call would overcount totals.

## Empty-session rule

A session is non-empty when a parsed turn contains user input, assistant output, or any numeric token value, including zero.
