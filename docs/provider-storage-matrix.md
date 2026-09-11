# Provider storage and viewer support matrix

This matrix describes where each provider normally stores conversations and what
`AIToolSessionTracker` can read locally.

| Provider | Conversation type | Platform / local surface | Normal storage model | Local source used by this project | Viewer support |
|---|---|---|---|---|---|
| GitHub Copilot | Code / Chat | Extension, CLI, Desktop | Local JSONL transcripts, session-state files, and local SQLite session stores | Extension storage under `%APPDATA%\\Code\\User` on Windows, `~/Library/Application Support/Code/User` on macOS, or `$XDG_CONFIG_HOME/Code/User` / `~/.config/Code/User` on Linux; plus `%USERPROFILE%\\.copilot` / `~/.copilot` | **Supported** |
| OpenAI Codex | Code / Chat | Extension, CLI, Desktop | Local JSONL rollout files | `~/.codex/sessions/**/*.jsonl` (`%USERPROFILE%\\.codex\\sessions` on Windows) | **Supported** |
| Anthropic Claude | Chat | Desktop | Primarily cloud-backed; local application cache is not a stable transcript interface | No supported local transcript source | **Not supported from cache** |
| Anthropic Claude | Code | Extension, CLI, Desktop | Local JSONL project transcripts and audit/session files for the desktop coding agent | `~/.claude/projects/**/*.jsonl`; platform application-data audit storage: `%LOCALAPPDATA%\\Claude-3p` on Windows, `~/Library/Application Support/Claude` on macOS, or `$XDG_CONFIG_HOME/Claude` on Linux | **Supported** |
| Google Antigravity | Code / Chat | IDE, Desktop (2.0), CLI | Local JSONL transcripts, brain artifacts, and local SQLite trajectory databases | `~/.gemini/antigravity-ide`, `~/.gemini/antigravity`, and `~/.gemini/antigravity-cli` on Windows, macOS, and Linux; `ANTIGRAVITY_ROOT` can override the root | **Supported** — CLI sessions are labeled **CLI** |
| Microsoft 365 Copilot | Chat | Microsoft 365 web, Teams, or Office | Cloud-backed | User-provided local JSON/JSONL exports only; `~/m365-copilot/sessions` by default on all platforms, or `M365_COPILOT_ROOT` when configured | **Supported for exports only** |

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
