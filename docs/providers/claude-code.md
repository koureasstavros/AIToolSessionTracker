# Anthropic Claude Code provider parsing

## Sources

The provider scans:

- `%USERPROFILE%\\.claude\\projects\\**\\*.jsonl`
- Claude Desktop audit files under `%LOCALAPPDATA%\\Claude-3p\\local-agent-mode-sessions\\**\\audit.jsonl`

The provider deduplicates entries by conversation ID and prefers a source with data, then the most recently updated source.

## Identity and metadata

- **GUID / ID:** Prefer `sessionId`, `session_id`, `conversationId`, or `conversation_id`. Fall back to the filename stem.
- **Name:** Prefer `title`, `name`, `summary`, or `conversationTitle`. If no title is persisted, derive a short name from the first real user message, excluding tool-result content. Otherwise use the session ID.
- **Datetime:** Use the transcript file modification time.
- **Model:** Use a model field found in the payload; fall back to `claude`.
- **Project:** Infer the project from transcript records. Desktop local-agent storage paths are not exposed as project directories.
- **Source:** Use the exact transcript or audit JSONL path.

## Turns

Records are grouped by turn identifiers such as `turn_id`, `turnId`, metadata turn IDs, `promptId`, or a user-record UUID fallback. A new user message after an existing populated turn starts a new logical turn, even when Claude reuses the same turn ID.

Tool activity is exposed separately:

- Each `tool_use` starts a turn marked `kind: tool`.
- Matching `tool_result` records stay with the active tool turn.
- Tool names and inputs are shown as readable assistant-side content.
- Tool results are shown as readable output rather than JSON user input.
- Pure tool-result records are not treated as new user prompts.
- Raw source records remain attached to the turn.

## Tokens

Usage is read from payload `usage`, message `usage`, `usageMetadata`, or nested `last_token_usage` fields. Recognized values are normalized to:

- `inputTokens`
- `cacheReadTokens`
- `cacheWriteTokens`
- `outputTokens`
- `reasoningTokens`

Per-turn values are summed into session totals. Missing usage remains unavailable rather than inferred from message length.

## Empty-session rule

A session is non-empty when a parsed turn contains user input, assistant output, or any numeric token value, including zero. Tool turns containing readable tool input/output also qualify as non-empty.
