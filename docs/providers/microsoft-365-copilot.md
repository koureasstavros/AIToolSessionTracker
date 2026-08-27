# Microsoft 365 Copilot provider parsing

## Sources

The provider reads local JSON and JSONL transcript exports from:

- `M365_COPILOT_ROOT`, when set
- `%USERPROFILE%\\m365-copilot\\sessions`, by default

Microsoft 365 Copilot's cloud conversation history and Office/WebView cache are not treated as stable transcript sources. The exact export path is retained as `_source` and displayed as the information source.

## Identity and metadata

- **GUID / ID:** Prefer `sessionId`, `session_id`, `conversationId`, or `conversation_id`. Fall back to the filename stem.
- **Name:** Prefer common title fields such as `title`, `name`, `conversationTitle`, or `subject`. Otherwise use the session ID.
- **Datetime:** Use a supported timestamp field from the export; fall back to the file modification time.
- **Model:** Prefer common model fields when present; otherwise leave unavailable.
- **Project:** Use a project/workspace field when the export contains one; otherwise leave unavailable.
- **Source:** Use the exact JSON or JSONL export path.

## Turns

The parser accepts common message containers, roles, nested content, and text fields. User content is assigned to the turn's user field; assistant/model content is appended to assistant output. Raw source records are retained.

If the export contains tool-call or tool-result records in a supported structure, they are retained as raw event data. The provider's primary contract is local transcript export parsing rather than a provider-specific tool protocol.

## Tokens

Common usage fields and aliases are normalized to:

- `inputTokens`
- `cacheReadTokens`
- `cacheWriteTokens`
- `outputTokens`
- `reasoningTokens`

Values are displayed when present in the export. Missing values are not fabricated. Session totals are calculated from parsed per-turn values when available.

## Empty-session rule

A session is non-empty when a parsed turn contains user input, assistant output, or any numeric token value, including zero.
