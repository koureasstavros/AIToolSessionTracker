# Session Token Viewer

A local, read-only browser app for exploring AI coding-agent sessions, turns, content, raw events, and token usage.

## Run

From this folder:

```text
python session_token_viewer.py
```

The app opens at `http://127.0.0.1:8765`.

Sidebar entries show the conversation name when the provider supplies one; otherwise they show the conversation ID. Every entry also shows its source and last-updated timestamp.

Each conversation also has a delete button. Deletion requires browser confirmation and removes the provider’s stored transcript or database records locally:

- Copilot and Codex/Claude file-backed sessions: removes the related transcript file.
- Legacy Copilot folders: removes the session folder and its contents.
- Copilot CLI database sessions: removes the session and related rows from `session-store.db`.

Deletion cannot be undone by this application.

When available, the session detail header shows the associated project directory between the session GUID and model information.

Optional arguments:

```text
python session_token_viewer.py --port 9000
python session_token_viewer.py --root C:\path\to\copilot\session-state
```

Stop the server with `Ctrl+C`.

## Supported providers

### GitHub Copilot

The VS Code session actions are archive and delete, which are two different things

The VS Code non-project-related workspace storage directory:

```text
%APPDATA%\Code\User\globalStorage\emptyWindowChatSessions\*.jsonl
```

The VS Code project-related workspace storage directory:

```text
%APPDATA%\Code\User\workspaceStorage\*\chatSessions\*.jsonl
```

This source does include `input_tokens` and `output_tokens` per-turn values.

Older session-state folders containing the following files are also supported:

```text
<root>\<session-id>\workspace.yaml
<root>\<session-id>\events.jsonl
```

Newer VSCode Copilot project and non-project-related sessions also maintain additional metadata in (but no usage details):

```text
%APPDATA%\Code\User\globalStorage\github.copilot-chat\session-store.db
```

This source does not include uage and therefore no per-turn values.

Newer CLI Copilot project and non-project-related sessions also maintain additional metadata in (and with usage details):

```text
%USERPROFILE%\.copilot\session-store.db
```

This source includes per-turn `cache_read_tokens`, `cache_write_tokens`, and `reasoning_tokens` values.


### OpenAI Codex

The VS Code session actions are archive, which is a delete operation

Reads Codex JSONL rollout files from:

```text
%USERPROFILE%\.codex\sessions\**\*.jsonl
```

The adapter understands Codex records such as:

- `response_item`
- `event_msg`
- `token_count`
- Nested `turn_id` metadata

### Anthropic Claude Code

The VS Code session actions are delete, which is a delete operation

Reads Codex JSONL files from:

```text
%USERPROFILE%\.claude\projects\**\*.jsonl
```

Claude Code Desktop local-agent transcripts:

```text
%LOCALAPPDATA%\Claude-3p\local-agent-mode-sessions\**\audit.jsonl
```

Metadata-only session files are ignored. Desktop transcripts are identified by their `session_id` values.

## What the app displays

The left-hand provider menu selects the data source. The session list then shows sessions for that provider.

### Interface

- The left sidebar stays fixed while the main session details scroll independently.
- The sidebar session list has its own styled vertical scrollbar and does not scroll horizontally.
- Use the refresh button above the session list to rescan provider data.
- The selected session header shows its GUID, associated project directory when available, model, and the exact transcript or database source path used to load it.
- The main content area shows token totals, turn cards, and the **Content Explorer** side panel.
- The layout adapts to smaller screens by returning to normal page scrolling.

Each session contains one or more interactions. An interaction is grouped into a turn containing:

- User input
- Assistant output
- Per-turn token metrics
- Raw event records

The supported token categories are:

- Input
- Input cache read
- Input cache write
- Output
- Output reasoning

Click a token card inside a turn to open the right-side **Content Explorer**. The explorer displays the relevant readable content and provides expandable raw event JSON.

## Token calculation

Provider formats expose different token information:

- GitHub Copilot session totals come from `session.shutdown.modelMetrics.usage`.
- Copilot output totals are recalculated from all `assistant.message.outputTokens` records, including resumed turns.
- Codex usage comes from `event_msg` records containing `token_count.info.last_token_usage`.
- Claude token values are displayed when usage fields are present in the transcript.
- For GitHub Copilot and OpenAI Codex, the displayed Input value excludes `cacheReadTokens` and `cacheWriteTokens`; cached input remains shown separately.

When a provider only stores input, cache, or reasoning usage at session level, the viewer estimates per-turn values using each turn's output-token share. The estimates preserve the exact session total and are labelled in the interface.

## Raw content and privacy

The app does not modify, upload, or transmit session files. It runs on `127.0.0.1` and reads files locally.

Raw event data may contain sensitive information, including:

- System and environment instructions
- File paths
- Tool calls and tool results
- Prompts and responses
- Encrypted reasoning payloads

Encrypted payloads can be displayed as raw text but cannot be decrypted by this app.

## Limitations

- Active sessions may not appear until their provider writes a transcript file.
- Temporary folders without event files are ignored.
- Provider-specific token fields may be unavailable or estimated.
- Claude Desktop browser/UI cache data is not parsed; the supported desktop source is `local-agent-mode-sessions\\**\\audit.jsonl`.
- The app is intentionally read-only.
