# Session Token Viewer

A local, read-only browser app for exploring AI coding-agent sessions, turns, content, raw events, and token usage.

## Run

From this folder:

```text
python session_token_viewer.py
```

The app opens at `http://127.0.0.1:8765`.

See [Provider storage and viewer support matrix](docs/provider-storage-matrix.md)
for the supported chat and code surfaces, storage locations, and local-read
limitations.

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

Provider-specific storage paths, formats, token behavior, and limitations are
documented separately:

- [GitHub Copilot](docs/providers/github-copilot.md)
- [OpenAI Codex](docs/providers/openai-codex.md)
- [Anthropic Claude Code](docs/providers/claude-code.md)
- [Microsoft 365 Copilot](docs/providers/microsoft-365-copilot.md)
- [Provider storage and viewer support matrix](docs/provider-storage-matrix.md)

The viewer only reads local transcripts or user-provided exports. It does not
download cloud-only chat history.

## Provider architecture

The application separates the provider-neutral viewer from provider-specific
storage and transcript formats.

### Main application

`session_token_viewer.py` owns the common application behavior:

- The normalized conversation, turn, and token structure.
- Provider registration and adapter dispatch.
- HTML rendering and the Content Explorer.
- HTTP request handling and command-line startup.
- Shared token fields and display labels.

Every provider returns conversations using the same normalized fields:

- `id`, `name`, `updated`, and `model`
- `project` and `source`
- `provider`, source label, source kind, and provider storage metadata
- `turns`, including user content, assistant content, and raw records
- `tokens`, containing:
	- `inputTokens`
	- `cacheReadTokens`
	- `cacheWriteTokens`
	- `outputTokens`
	- `reasoningTokens`

The main application normalizes provider results before passing them to the
interface. Rendering therefore does not need to understand each provider's
native file or database format.

### Provider adapters

Provider-specific behavior is implemented in:

- `src/providers/github_copilot_provider.py`
- `src/providers/openai_codex_provider.py`
- `src/providers/anthropic_claude_provider.py`
- `src/providers/m365_copilot_provider.py`

Each provider adapter under `src/providers/` exposes the same operations:

- `index(root)` discovers conversations and returns inexpensive sidebar summaries.
- `details(summary)` loads and parses one complete conversation.
- `delete(summary)` removes the provider's local conversation data.
- `display_root(root)` returns the source location displayed in the sidebar.
- `identity(record, fallback)` resolves the provider's conversation ID and name.
- `tool(summary)` identifies the originating surface, such as CLI, Extension, or Desktop.

Adapters are registered in `PROVIDER_ADAPTERS`. The main application selects an
adapter by provider ID and calls these common operations without implementing
provider-specific parsing or deletion rules.

Deletion remains abstract in the main application: it validates the selected
conversation and delegates the actual operation to the owning adapter. For
example, file-backed providers unlink transcript files, legacy Copilot removes
the session directory, and Copilot CLI removes related database rows.

## What the app displays

The left-hand provider menu selects the data source. The session list then shows sessions for that provider.

### Interface

- The left sidebar stays fixed while the main session details scroll independently.
- The sidebar session list has its own styled vertical scrollbar and does not scroll horizontally.
- Use the refresh button above the session list to rescan provider data.
- Use **Show empty** or **Hide empty** beside refresh to control whether sessions without a meaningful turn appear. A session is non-empty when at least one turn has user input, assistant output, or a numeric token value (including zero). The preference is preserved while switching providers and inspecting token content.
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

Click a token card inside a turn to open the right-side **Content Explorer**. The explorer displays only the relevant readable content. Use the horizontal **Show Raw** button below the token cards to display that turn's raw event data. Navigating with either a token card or **Show Raw** preserves the current detail scroll position.

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
