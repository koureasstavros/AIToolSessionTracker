# OpenAI Codex provider parsing

## Sources

The provider scans:

- Extension / CLI / Desktop `%USERPROFILE%\\.codex\\sessions\\**\\*.jsonl`

The exact rollout path is retained as `_source` and displayed as the information source. The source label is OpenAI Codex and the tool surface is reported as Extension, CLI and Desktop.

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

One user request is displayed as one logical turn. Records are associated using
`turn_id`, `turnId`, nested item turn IDs, `promptId`, task boundaries, or a
record UUID fallback. User and assistant messages are assigned from their roles
and readable content.

Within a turn, each model call is displayed as a numbered invocation. A
`token_count` event closes the current invocation, and the next assistant
message or tool batch begins another invocation. Multi-invocation and
tool-using requests show the complete invocation breakdown. A single
invocation without tools is not expanded
separately because the same usage is already visible at turn level.

Codex function calls are grouped under their owning invocation:

- `call_id` pairs each `function_call` with its `function_call_output`.
- Parallel calls remain separate vertically listed tools within the same invocation.
- Tool names, arguments, status, and stored results are available by expanding
	the tool row.
- Raw records remain attached to the logical turn.

Metadata-only records such as initial queue/attachment records are not displayed as turns.

## Tokens

Usage is read primarily from `event_msg` records containing `token_count.info.last_token_usage`, plus recognized usage aliases. Normalized fields are:

- `inputTokens`
- `cacheReadTokens`
- `cacheWriteTokens`
- `outputTokens`
- `reasoningTokens`

Each `token_count.info.last_token_usage` record supplies exact metrics for one
invocation. Invocation metrics are grouped in the UI as **User / Input** and
**Assistant / Output**, then summed into the logical turn and session totals.
Token information is not attributed to individual tools because tool call and
result records do not contain separate usage. Missing values are not inferred
or copied; duplicating an invocation's usage across its tools would overcount totals.

## Empty-session rule

A session is non-empty when a parsed turn contains user input, assistant output, or any numeric token value, including zero.
