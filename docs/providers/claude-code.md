# Anthropic Claude Code provider parsing

## Sources

The provider scans:

- Extension / CLI / Desktop `%USERPROFILE%\\.claude\\projects\\**\\*.jsonl`
- Extension / CLI / Desktop session metadata `%USERPROFILE%\\.claude\\sessions\\*.json`
- Desktop audit records `%LOCALAPPDATA%\\Claude-3p\\local-agent-mode-sessions\\**\\audit.jsonl`

The exact transcript or audit path is retained as `_source` and displayed as the
information source. The source label is Anthropic Claude Code, and the tool
surface is reported as Extension, CLI, or Desktop when the storage location
provides enough evidence.

Claude Code, the VS Code integration, and Desktop may share the same `.claude`
transcript locations. The viewer reads the JSONL records and labels sessions
without identifying metadata `Mixed` rather than claiming a single surface. The provider
deduplicates entries by conversation ID and prefers a source with data, then
the most recently updated source.

## Surface identification

- Desktop sessions are identified by the `Claude-3p\\local-agent-mode-sessions` path,
	a matching `*.desktop-released.json` marker, or explicit Desktop metadata.
- Session or transcript metadata with `entrypoint: claude-vscode` is labeled
	**Extension**; CLI and Desktop entrypoints are labeled accordingly when
	present.
- If the same session ID has evidence from multiple surfaces, the UI labels it
	`Mixed` instead of choosing one source arbitrarily.
- Sessions under `.claude\\projects` or `.claude\\sessions` may originate from
	Claude Code CLI, the VS Code integration, or Desktop because those surfaces
	can share the same transcript locations. The viewer labels them with the
	explicit surface when metadata is available and `Mixed` otherwise.

## Actions

Claude Code exposes **Delete** for local transcripts. The viewer exposes
**Delete** and removes the selected Claude Code JSONL transcript or Desktop
`audit.jsonl` file. Session registry and Desktop release-marker metadata are
not treated as transcripts. This is a local file operation and does not delete
ordinary cloud-backed Claude Chat history.

Imported transcripts are written to `%USERPROFILE%\\.claude\\projects\\imported`.
Existing filenames are preserved when possible; filename collisions receive a
generated suffix. Imported archives are validated for provider ownership and
path traversal before any file is written.

## Identity and metadata

- **GUID / ID:** Prefer `sessionId`, `session_id`, `conversationId`, or `conversation_id`. Fall back to the filename stem.
- **Name:** Prefer `title`, `name`, `summary`, or `conversationTitle`. If no title is persisted, derive a short name from the first real user message, excluding tool-result content. Otherwise use the session ID.
- **Datetime:** Use the transcript file modification time.
- **Model:** Use a model field found in the payload; fall back to `claude`.
- **Project:** Infer the project from transcript records. Desktop local-agent storage paths are not exposed as project directories.
- **Source:** Use the exact transcript or audit JSONL path.

## Turns

Each real user prompt starts a logical turn. Tool-result records serialized
with the `user` role remain part of the active turn and are not treated as new
prompts. Explicit turn identifiers are preferred, with the user-record UUID or
a generated identifier used as a fallback.

Each unique Claude assistant message ID becomes a numbered model invocation.
Claude can persist the text and `tool_use` portions of one API response as
separate records with the same message ID; the viewer combines those records
into the same invocation and counts their shared usage only once.

Tool activity is nested under its owning invocation:

- Each `tool_use` adds a vertically listed tool to the current invocation.
- Its ID links the matching `tool_result` back to that tool and invocation.
- Tool names, inputs, completion status, and results are expandable.
- Pure tool-result records are not treated as new user prompts.
- Raw source records remain attached to the logical turn.

A final assistant response without tools remains a separate invocation when
the turn contains multiple invocations. A single no-tool invocation is not expanded because
its metrics are already shown at turn level.

## Tokens

Usage is read from payload `usage`, message `usage`, `usageMetadata`, or nested `last_token_usage` fields. Recognized values are normalized to:

- `inputTokens`
- `cacheReadTokens`
- `cacheWriteTokens`
- `outputTokens`
- `reasoningTokens`

Usage attached to an assistant message is displayed on that exact invocation,
grouped as **User / Input** and **Assistant / Output**. Invocation values are
summed into turn and session totals. Tool calls do not receive separate token
counts because Claude reports usage for the model response, not for each tool.
Missing usage remains unavailable rather than inferred from message length.

## Empty-session rule

A session is non-empty when a parsed turn contains user input, assistant output,
tool activity, or any numeric token value, including zero.
