# GitHub Copilot provider parsing

## Sources

The provider scans:

- VS Code project sessions: `%APPDATA%\\Code\\User\\workspaceStorage\\*\\chatSessions\\*.jsonl`
- VS Code non-project sessions: `%APPDATA%\\Code\\User\\globalStorage\\emptyWindowChatSessions\\*.jsonl`
- Legacy session-state folders containing `workspace.yaml` and `events.jsonl`
- Copilot CLI metadata from `%USERPROFILE%\\.copilot\\session-store.db`

A source path is retained in `_source` and displayed as the information source. The source label identifies VS Code, Copilot legacy, or Copilot CLI.

## Identity and metadata

- **GUID / ID:** For VS Code JSONL, use the first record's `sessionId`; fall back to the filename stem. For CLI database records, use the database session ID.
- **Name:** Prefer `customTitle`, including a later `customTitle` record. Fall back to the session ID. Rollout-like or missing names are displayed as the ID.
- **Datetime:** Use the transcript file modification time. For CLI database sessions, use `updated_at`; when unavailable, use the database file modification time.
- **Model:** Prefer a request `modelId`. CLI sessions use the stored model metadata when available; otherwise display `model unavailable` or the provider fallback.
- **Project:** Prefer `folder`, `workspaceFolder`, or `projectPath`. If absent, infer the VS Code workspace from the parent workspace-storage path. CLI sessions use the stored working-directory/project column when available.
- **Source:** Use the exact JSONL or database path that supplied the conversation.

## Turns

The parser reconstructs requests from the initial `requests` array and later JSONL patch records. A request can contain:

- User input from `message.text`
- Assistant output from response items with readable `value` text
- Raw request/response JSON
- Request-level usage metadata

Requests without user text, assistant text, or token values are ignored when details are loaded. Serialized `toolInvocationSerialized` response items are exposed as separate turns marked `kind: tool`. Each tool turn includes the tool ID, invocation message, result output when available, and raw serialized event data.

## Tokens

Supported normalized fields are:

- `inputTokens`
- `cacheReadTokens`
- `cacheWriteTokens`
- `outputTokens`
- `reasoningTokens`

Input/output values come from request fields such as `promptTokens` and `completionTokens`, result metadata, and recognized usage aliases. Session totals are aggregated from turns. Cached input is subtracted from total input where the source reports both values.

The VS Code chat-session source often does not persist usage statistics. In that case, prompts, responses, and tool events remain visible while token fields remain unavailable.

## Empty-session rule

A session is non-empty when a parsed turn contains user input, assistant output, or any numeric token value, including zero. The `Show empty` toggle controls sessions that contain none of those values.
