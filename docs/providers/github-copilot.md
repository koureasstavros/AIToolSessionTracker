# GitHub Copilot provider parsing

## Sources

The provider scans:

- Extension sessions: `%APPDATA%\\Code\\User\\globalStorage\\github.copilot-chat\\session-store.db`
- Extension project sessions and metadata: `%APPDATA%\\Code\\User\\workspaceStorage\\*\\chatSessions\\*.jsonl`
- Extension non-project sessions and metadata: `%APPDATA%\\Code\\User\\globalStorage\\emptyWindowChatSessions\\*.jsonl`
- Copilot Desktop / CLI session and metadata from `%USERPROFILE%\\.copilot\\session-store.db`
- Copilot Desktop / CLI session and metadata from `%USERPROFILE%\.copilot\session-state\<session-id>\` containing `workspace.yaml`, `events.jsonl`, and optional per-session database files

The source path is retained in `_source` and displayed as the information source. The source label identifies VS Code, Copilot session-state, or the CLI / Desktop database.
When the same session ID exists in more than one source, all matching sources
are read. Session-state events provide the conversation content and the local
database supplements metadata, messages, and usage fields when they are
available; neither source is discarded during indexing.

The Extension JSONL source includes per-turn input and output values when the
transcript persists them. The VS Code SQLite metadata source does not provide
usage values. Copilot CLI database records can include cache-read,
cache-write, and reasoning-token values.

## Actions

The VS Code Copilot session actions are **Archive** and **Delete**; these are
different operations in VS Code. The viewer exposes **Delete** only. It removes
the selected JSONL transcript, removes the complete session-state folder, or
deletes the CLI database session and its related rows.

## Identity and metadata

- **GUID / ID:** For VS Code JSONL, use the first record's `sessionId`; fall back to the filename stem. For CLI database records, use the database session ID.
- **Name:** Prefer `customTitle`, including a later `customTitle` record. Fall back to the session ID. Rollout-like or missing names are displayed as the ID.
- **Datetime:** Use the transcript file modification time. For CLI database sessions, use `updated_at`; when unavailable, use the database file modification time.
- **Model:** Prefer a request `modelId`. CLI sessions use the stored model metadata when available; otherwise display `model unavailable` or the provider fallback.
- **Project:** Prefer `folder`, `workspaceFolder`, or `projectPath`. If absent, infer the VS Code workspace from the parent workspace-storage path. CLI sessions use the stored working-directory/project column when available.
- **Source:** Use the exact JSONL or database path that supplied the conversation.

## Surface identification

- Extension JSONL sessions are labelled `Extension`.
- Session-state folders are labelled `CLI / Desktop`, because these local
  Copilot session sources are written by the CLI/Desktop storage family and do
  not contain a reliable marker to distinguish the two surfaces.
- Sessions from the local `.copilot\\session-store.db` are labelled `CLI / Desktop`:
	the database location identifies the local Copilot store, but the records do
	not contain a reliable client marker to distinguish CLI from Desktop.

## Turns

GitHub Copilot uses source-specific turn handling:

- **VS Code JSONL:** one logical turn is created for each request in the
  `requests` array, including requests reconstructed from later JSONL patch
  records. Persisted `toolCallRounds` become numbered invocations. Tools
  are listed vertically inside the round that initiated them, including their
  arguments and stored results.
- **CLI / Desktop session-state:** events are grouped by `interactionId`, or by
  the linked `turnId`. Internal assistant turns become invocations, and
  tool start/completion events are attached to the assistant invocation that
  initiated them.
- **CLI / Desktop SQLite:** multiple assistant usage rows become invocations
  inside their logical user turn. Usage-only records can still become
  synthetic turns when no matching conversation content exists.

A request or event group can contain:

- User input from `message.text`
- Assistant output from response items with readable `value` text
- Numbered model invocations
- Tool names, arguments, completion status, and stored results
- Raw request/response JSON
- Request-level usage metadata

Requests without user text, assistant text, or token values are ignored when
details are loaded. A single invocation without tools is not expanded separately
because its usage is already shown by the turn.

## Tokens

Supported normalized fields are:

- `inputTokens`
- `cacheReadTokens`
- `cacheWriteTokens`
- `outputTokens`
- `reasoningTokens`

Input/output values come from request fields such as `promptTokens` and
`completionTokens`, result metadata, database usage rows, and recognized usage
aliases. Session totals are aggregated from turns. Cached input is subtracted
from total input where the source reports both values.

Invocation metrics are grouped as **User / Input** and **Assistant / Output**.
For session-state plus SQLite sessions, database usage rows provide invocation
values and event boundaries associate tools with those invocations. For VS Code
JSONL, `toolCallRounds` define the invocations. The source can expose exact input/output
metrics for the final no-tool round while retaining only aggregate totals for
the complete request; earlier tool rounds remain unavailable rather than being
assigned estimated values.

The VS Code chat-session source often does not persist usage statistics. In that case, prompts, responses, and tool events remain visible while token fields remain unavailable.

Token availability is not uniform across GitHub Copilot turns and invocations.
A text-bearing invocation may have `null` token fields. Tools never inherit or
duplicate their parent invocation's usage because Copilot does not report tokens per individual
tool. Session totals use persisted usage, apart from the documented
session-state allocation.

## Empty-session rule

A session is non-empty when a parsed turn contains user input, assistant output, or any numeric token value, including zero. The `Show empty` toggle controls sessions that contain none of those values.
