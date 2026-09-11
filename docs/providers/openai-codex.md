# OpenAI Codex provider parsing

## Sources

The provider scans:

- Extension / CLI / Desktop `~/.codex/sessions/**/*.jsonl`.

`Path.home()` resolves this location on Windows, macOS, and Linux, so no
platform-specific path configuration is required.

The exact rollout path is retained as `_source` and displayed as the information source. The source label is OpenAI Codex and the tool surface is reported as `Extension`, `CLI`, `Desktop`, or `Mixed`.

Codex source attribution uses transcript metadata such as `originator` and
`source`: `codex_vscode`/`vscode` identifies the extension,
`codex_work_desktop` identifies Desktop, and CLI markers identify the CLI. If
metadata is absent or conflicting, the UI uses `Mixed` rather than guessing.
The terminal originator `codex-tui` is also treated as CLI.

The same local rollout storage can contain both Codex coding sessions and Codex
chat conversations created through the extension, CLI or the Desktop. The
viewer reads both types from the JSONL transcript records; no separate chat
storage location is required.

## Actions

Codex exposes **Archive**, which functions as a delete operation for the local
rollout. The viewer exposes **Delete** and removes the selected JSONL rollout
file. This only removes the local transcript; it does not affect any remote
account data.

Imported transcripts are written to `%USERPROFILE%\\.codex\\sessions\\imported`.
Existing filenames are preserved when possible; filename collisions receive a
generated suffix. Imported archives are validated for provider ownership and
path traversal before any file is written.

## Identity and metadata

- **GUID / ID:** Prefer `sessionId` or `session_id`. If the value contains a UUID, expose the UUID portion. Otherwise use the filename stem.
- **Name:** Prefer non-rollout values from `title`, `name`, `summary`, or `session_name`. If no title is persisted, derive a short name from the first real user message. Otherwise use the normalized session ID.
- **Datetime:** Use the rollout file modification time.
- **Model:** Prefer a model field found in the records. Fall back to `codex`.
- **Project:** Infer the project from recognized project/workspace records.
- **Source:** Use the exact rollout JSONL path.

## Turns

One user request is displayed as one logical turn. Records are associated using
`turn_id`, `turnId`, nested item turn IDs, `promptId`, task boundaries, or a
record UUID fallback. User and assistant messages are assigned from their roles
and readable content.

Within a turn, each model call is displayed as a numbered invocation. A
`token_count` event closes the current invocation, and the next assistant
message or tool batch begins another invocation. Multi-invocation and
tool-using requests show the complete invocation breakdown. A single
invocation without tools is not expanded
separately because the same usage is already visible at turn level.

Codex function calls are grouped under their owning invocation:

- `call_id` pairs each `function_call` with its `function_call_output`.
- Parallel calls remain separate vertically listed tools within the same invocation.
- Tool names, arguments, status, and stored results are available by expanding
	the tool row.
- Raw records remain attached to the logical turn.

Spawned-agent rollouts are identified through
`source.subagent.thread_spawn.parent_thread_id` and removed from the root
session list when their parent rollout is available. A successful
`spawn_agent` result supplies the child `agent_id`, which links the child
rollout and its usage to that exact tool. Parent orchestration usage is shown
separately, each child has its own expandable usage, and the invocation total
combines the parent with all linked descendants.

Metadata-only records such as initial queue/attachment records are not displayed as turns.

## Tokens

Usage is read primarily from `event_msg` records containing `token_count.info.last_token_usage`, plus recognized usage aliases. Normalized fields are:

- `inputTokens`
- `cacheReadTokens`
- `cacheWriteTokens`
- `outputTokens`
- `reasoningTokens`

Each `token_count.info.last_token_usage` record supplies exact metrics for one
invocation. Invocation metrics are grouped in the UI as **User / Input** and
**Assistant / Output**, then summed into the logical turn and session totals.
Token information is not attributed to ordinary tools because tool call and
result records do not contain separate usage. A `spawn_agent` tool is the
exception when its returned `agent_id` resolves to a separate child rollout;
that rollout's independently reported usage is displayed under the tool.
Missing values are not inferred or copied; duplicating an invocation's usage
across its tools would overcount totals.

## Empty-session rule

A session is non-empty when a parsed turn contains user input, assistant output, or any numeric token value, including zero.
