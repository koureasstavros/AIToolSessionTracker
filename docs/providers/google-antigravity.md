# Google Antigravity provider parsing

## Sources

The provider scans:

- Antigravity IDE: `%USERPROFILE%\.gemini\antigravity-ide\brain\*\.system_generated\logs\transcript.jsonl` (reported in the viewer as **IDE**)
- Antigravity (Desktop): `%USERPROFILE%\.gemini\antigravity\brain\*\.system_generated\logs\transcript.jsonl`
- Antigravity CLI: `%USERPROFILE%\.gemini\antigravity-cli\**\*.jsonl` (reported in the viewer as **CLI**)
- Imported archives: `%USERPROFILE%\.gemini\antigravity-ide\brain\imported\**\*.jsonl`
- Custom storage roots configured via `--root` or the `ANTIGRAVITY_ROOT` environment variable.

The exact transcript path is retained as `_source` and displayed as the information source. The tool surface is reported as `IDE`, `Desktop`, or `CLI` based on the path, so sessions discovered below `antigravity-ide` are identified as **IDE**, while sessions below `antigravity-cli` are identified as **CLI**. Companion SQLite databases in `~/.gemini/antigravity-ide/conversations/<id>.db` or `~/.gemini/antigravity/conversations/<id>.db` are read to extract authoritative project roots and active model names.

## Actions

- **Export source files**: Archives the session's `transcript.jsonl`, `transcript_full.jsonl`, and brain artifacts into a ZIP archive with a standard `manifest.json`.
- **Import source files**: Injects an Antigravity ZIP archive into the configured destination under `imported/`, checking provider ownership and verifying path safety.
- **Delete**: Removes the conversation directory from `brain/` and unlinks the matching conversation SQLite database in `conversations/`. Deletion is local and irreversible.

Imported transcripts are written to the `imported` subfolder of
`ANTIGRAVITY_ROOT`, or to `%USERPROFILE%\\.gemini\\antigravity-ide\\brain\\imported`
by default. Existing filenames are preserved when possible; filename
collisions receive a generated suffix.

## Identity and metadata

- **GUID / ID:** Uses the conversation UUID folder name under `brain/` or the transcript filename stem.
- **Name:** Prefers user prompt extracted from `<USER_REQUEST>` tags in the first `USER_INPUT` step, or top headers from `task.md` / `implementation_plan.md`. Falls back to the conversation ID.
- **Datetime:** Uses the transcript file modification time.
- **Model:** Extracted from `gen_metadata` in companion SQLite databases or settings change records in the transcript (e.g. `gemini-3.8-flash`, `gemini-3.8-pro`, `gemini-3.7-flash`, `gemini-3.6-flash`, `gemini-3.5-flash`, `gemini-3.5-flash-lite`, `gemini-3.1-flash-lite`, `gemini-2.5-flash-lite`, `gemini-2.5-pro`, `Gemini 3.8 Flash (High)`). Falls back to `gemini-3.8-flash`.
- **Project:** Extracted from `trajectory_metadata_blob` in companion SQLite databases or workspace markers in the initial user turn.
- **Source:** The exact transcript JSONL path.

## Turns

One user request (`USER_INPUT`) is displayed as one logical turn. The user prompt is cleaned to remove internal XML wrappers (`<USER_REQUEST>`, `<ADDITIONAL_METADATA>`, etc.). Active document references and `@`-mentioned files are normalized and displayed as attached files.

Within a turn:
- Each model interaction (`PLANNER_RESPONSE`) begins an invocation.
- Thinking blocks (`thinking` field) are preserved and associated with the model invocation.
- Tool calls (`list_dir`, `run_command`, `view_file`, `browser_subagent`, etc.) are captured with their names and JSON arguments.
- Subsequent tool result steps (`LIST_DIRECTORY`, `RUN_COMMAND`, `VIEW_FILE`, etc.) are correlated with the tool calls, tracking completion status and result payloads.
- Subagents: Tools invoking subagents (e.g., `browser_subagent`, `invoke_subagent`) are flagged as subagent invocations.

## Tokens and Pricing

Antigravity executes against Google backend APIs that stream conversation states and tool events to the client. Because numeric token counters are not persisted directly into local `transcript.jsonl` step objects, the provider applies an accurate character-based heuristic (~4 characters per token) across:
- **Input tokens**: User prompt text, attached file contents, tool invocation arguments, and accumulated tool results feeding back into subsequent turns.
- **Output tokens**: Model planner responses, tool arguments, and reasoning/thinking steps.
- **Reasoning tokens**: Extracted directly from model `thinking` properties.

Token counts are priced using Google Gemini model rates defined in `src/common/model_costs.json` (including Flash, Flash-Lite, and Pro variants from Gemini 2.5 through 3.8). In the timeline header, turns show the explanatory footnote `· token metrics estimated from transcript content`. In aggregate views (Statistics by Tool, Model, Project, Day, and Timeline), these estimates allow complete cross-provider comparison, cost tracking, and activity analysis.

## Empty-session rule

A session is non-empty when it contains at least one turn with user input, planner output, or tool execution.
