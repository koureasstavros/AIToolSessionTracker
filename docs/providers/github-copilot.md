# GitHub Copilot provider parsing

## Sources

The provider scans:

- Extension sessions: `<Extension storage>/globalStorage/github.copilot-chat/session-store.db`
- Extension project sessions and metadata: `<Extension storage>/workspaceStorage/*/chatSessions/*.jsonl`
- Extension non-project sessions and metadata: `<Extension storage>/globalStorage/emptyWindowChatSessions/*.jsonl`
- Copilot Desktop / CLI session and metadata from `~/.copilot/session-store.db`
- Copilot Desktop / CLI session and metadata from `~/.copilot/session-state/<session-id>/` containing `workspace.yaml`, `events.jsonl`, and optional per-session database files

`<Extension storage>` is `%APPDATA%/Code/User` on Windows,
`~/Library/Application Support/Code/User` on macOS, and
`$XDG_CONFIG_HOME/Code/User` or `~/.config/Code/User` on Linux. The equivalent
`Code - Insiders/User` extension storage is also checked. `Path.home()` resolves the
`~/.copilot` locations on all supported platforms.

The source path is retained in `_source` and displayed as the information source. The source label identifies `Extension` for VS Code chat files. Session-state `workspace.yaml` files can identify CLI sessions through `client_name: github/cli`, and Desktop sessions through `client_name: github/autopilot`. When that marker is absent, the shared session-state and database sources are labelled `Mixed` because they can contain CLI and Desktop sessions without a reliable client identifier.
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

Imported file-backed sessions are written to the configured workspace storage
root's `imported` folder. Copilot database rows are not exported because a
database copy cannot be safely merged into the live session store. Imported
archives are validated for provider ownership and path traversal before any
file is written.

## Identity and metadata

- **GUID / ID:** For VS Code JSONL, use the first record's `sessionId`; fall back to the filename stem. For CLI database records, use the database session ID.
- **Name:** Prefer `customTitle`, including a later `customTitle` record. Fall back to the session ID. Rollout-like or missing names are displayed as the ID.
- **Datetime:** Use the transcript file modification time. For CLI database sessions, use `updated_at`; when unavailable, use the database file modification time.
- **Model:** Prefer a request `modelId`. CLI sessions use the stored model metadata when available; otherwise display `model unavailable` or the provider fallback.
- **Project:** Prefer `folder`, `workspaceFolder`, or `projectPath`. If absent, infer the VS Code workspace from the parent workspace-storage path. CLI sessions use the stored working-directory/project column when available.
- **Source:** Use the exact JSONL or database path that supplied the conversation.

## Surface identification

- Extension JSONL sessions are labelled `Extension`.
- Session-state folders with `client_name: github/cli` are labelled `CLI`.
  Folders with `client_name: github/autopilot` are labelled `Desktop`. Other session-state
  folders are labelled `Mixed`, because these local Copilot session sources
  are written by the CLI/Desktop storage family and may not contain a reliable
  marker to distinguish the two surfaces.
- Sessions from the local `.copilot\\session-store.db` are labelled `Mixed`:
	the database location identifies the local Copilot store, but the records do
	not contain a reliable client marker to distinguish CLI from Desktop.

## Turns

GitHub Copilot uses source-specific turn handling:

- **VS Code JSONL:** one logical turn is created for each request in the
  `requests` array, including requests reconstructed from later JSONL patch
  records. Persisted `toolCallRounds` become numbered invocations. Tools
  are listed vertically inside the round that initiated them, including their
  arguments and stored results. Serialized `runSubagent` tools become nested
  agent cards using their description, agent name, model, prompt, and result.
- **CLI / Desktop session-state:** events are grouped by `interactionId`, or by
  the linked `turnId`. Internal assistant turns become invocations, and
  tool start/completion events are attached to the assistant invocation that
  initiated them.
- **CLI / Desktop SQLite:** multiple assistant usage rows become invocations
  inside their logical user turn. Usage-only records can still become
  synthetic turns when no matching conversation content exists.

CLI/Desktop subagent usage rows expose `parent_tool_call_id`. The viewer uses
that identifier to move child interactions under the exact `task` tool that
spawned them. Parent orchestration usage remains separately expandable, while
the invocation total combines parent and linked child usage.

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
This also applies to `runSubagent`: when the child request's token usage is not
persisted, the child card explicitly reports **Token usage unavailable** and
the invocation rollup is marked as partial rather than estimating or dividing
the request-level total among agents.

Token availability is not uniform across GitHub Copilot turns and invocations.
A text-bearing invocation may have `null` token fields. Tools never inherit or
duplicate their parent invocation's usage because Copilot does not report tokens per individual
tool. Session totals use persisted usage, apart from the documented
session-state allocation.

## Empty-session rule

A session is non-empty when a parsed turn contains user input, assistant output, or any numeric token value, including zero. The `Show empty` toggle controls sessions that contain none of those values.
