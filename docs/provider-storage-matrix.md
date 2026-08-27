# Provider storage and viewer support matrix

This matrix describes where each provider normally stores conversations and what
`AIToolSessionTracker` can read locally.

| Provider | Conversation type | Platform / local surface | Normal storage model | Local source used by this project | Viewer support |
|---|---|---|---|---|---|
| GitHub Copilot | Code / Chat | Extension, CLI, Desktop | Local JSONL transcripts, legacy session-state files, and local SQLite session stores | `%APPDATA%\\Code\\User\\globalStorage\\emptyWindowChatSessions\\*.jsonl`; `%APPDATA%\\Code\\User\\workspaceStorage\\*\\chatSessions\\*.jsonl`; `%USERPROFILE%\\.copilot\\session-state\\*\\events.jsonl`; `%APPDATA%\\Code\\User\\workspaceStorage\\*\\events.jsonl`; `%APPDATA%\\Code\\User\\globalStorage\\github.copilot-chat\\session-store.db`; `%USERPROFILE%\\.copilot\\session-store.db` | **Supported** |
| OpenAI Codex | Code / Chat | Extension, CLI, Desktop | Local JSONL rollout files | `%USERPROFILE%\\.codex\\sessions\\**\\*.jsonl` | **Supported** |
| Anthropic Claude | Chat | Desktop | Primarily cloud-backed; local application cache is not a stable transcript interface | No supported local transcript source | **Not supported from cache** |
| Anthropic Claude | Code | Extension, CLI, Desktop | Local JSONL project transcripts and audit/session files for the desktop coding agent | `%USERPROFILE%\\.claude\\projects\\**\\*.jsonl`; `%LOCALAPPDATA%\\Claude-3p\\local-agent-mode-sessions\\**\\audit.jsonl` | **Supported** |
| Microsoft 365 Copilot | Chat | Microsoft 365 web, Teams, or Office | Cloud-backed | User-provided local JSON/JSONL exports only; `%USERPROFILE%\\m365-copilot\\sessions` by default | **Supported for exports only** |

## Storage categories

- **Local transcript:** The message and tool records are available on the
  computer in JSON or JSONL files.
- **Cloud-backed:** The provider keeps the conversation history in the account
  service. Local cache files should not be treated as a reliable archive.
- **Export-only:** The provider does not expose a supported local transcript
  source; the viewer can read a user-created export.

## Scope of this project

The viewer is local and read-only. It does not connect to provider cloud APIs,
download account history, or parse browser/application caches. For a provider
whose history is cloud-backed, provide a supported JSON/JSONL export before
expecting it to appear in the viewer.
