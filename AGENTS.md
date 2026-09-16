# Repository Guidelines

## Project Structure & Module Organization

This repository is a local, read-only Python web app for exploring AI coding-agent sessions. The main entry point is `session_token_viewer.py`. Provider-specific discovery, parsing, deletion, and archive logic belongs in `src/providers/`; keep provider-neutral behavior in the main application. Tests are in `tests/`, provider behavior and storage notes are in `docs/`, and screenshots or other visual assets are in `material/`.

## Build, Test, and Development Commands

No build system or third-party dependency manifest is currently defined. Use the Python standard library from the repository root:

```text
python session_token_viewer.py
python session_token_viewer.py --port 9000
python -m unittest discover -s tests -p "test_*.py"
```

The first command starts the viewer at `http://127.0.0.1:8765`; `--port` changes the listening port. The unittest command runs the complete test suite. Stop the local server with `Ctrl+C`.

## Coding Style & Naming Conventions

Use Python 3 conventions, four-space indentation, descriptive `snake_case` names for functions and variables, and `PascalCase` for classes. Follow the existing module layout and keep provider adapters behind the shared adapter interface. Prefer small helpers and explicit error handling for local files, JSON, ZIP archives, and SQLite data. No formatter or linter is configured, so keep changes PEP 8-compatible and review diffs manually.

## Testing Guidelines

Tests use `unittest` and are named `test_*.py`; test classes use descriptive `PascalCase` names ending in `Tests` or `Test`. Add focused regression tests for provider parsing, token grouping, deletion, and archive validation. Run the full discovery command before submitting changes.

## Commit & Pull Request Guidelines

Recent commits use short, task-focused summaries such as `Fixing and testing import / export` and `Added search filter and import/export functionality`; continue using concise descriptions that identify the behavior changed. Pull requests should explain the user-visible impact, list validation commands, link a related issue when applicable, and include screenshots for interface changes. Document provider-specific storage or behavior changes in the matching file under `docs/providers/` and update `README.md` when usage changes.

## Security & Privacy Tips

The app reads sensitive local transcripts and must remain local-only. Do not add telemetry, cloud uploads, or sample session data containing real prompts, paths, tool results, or secrets. Preserve archive path-traversal validation and treat deletion behavior as destructive and irreversible.

## Session Scanning and Empty Sessions

Keep the initial sidebar scan lightweight. Provider indexes should return basic
session metadata and an `_has_data` classification without parsing full
transcripts; load complete details only when a session is selected or a view
explicitly requires usage details. The progressive scan must update the sidebar
and detail content without replacing the entire `.app` shell, so existing
controls, filters, scroll positions, and click handlers remain stable.

The normal sidebar (`show_empty=0`) must include only summaries with
`_has_data=True`. Empty or unclassified summaries remain available through
`show_empty=1`. A session is non-empty when it has meaningful user content,
assistant content, or numeric token usage, including zero; tool-only activity
does not make a session non-empty. For GitHub Copilot, classify session-state
events and database turns/usage rows during indexing so empty database rows do
not appear in the normal sidebar. Preserve `_has_data` when merging multiple
Copilot sources for the same session.

After lightweight indexing, a separate background worker may warm complete
details into the summary cache one session at a time. Operational session
selection and Statistics should reuse that cache rather than reparsing the
same transcript. A full sidebar refresh invalidates the scan and detail cache;
the per-session refresh control invalidates only the selected session's cached
details. Do not make the initial sidebar request wait for background warming.

### Loading and Cache Reuse Approach

Treat session loading as two distinct phases:

1. **Light loading:** provider `index(root)` operations discover IDs, names,
	 timestamps, source/surface, project/model metadata when inexpensive, and
	 `_has_data`. They must avoid building turns, invocations, tool payloads, raw
	 events, or complete token breakdowns. Light summaries populate the sidebar
	 and support filtering, sorting, empty-session visibility, and selection.
2. **Heavy loading:** provider `details(summary)` parses the complete local
	 transcript or database representation. It may build turns, invocations,
	 tools, delegated agents, raw content, usage, costs, and effort metadata.
	 Heavy loading happens on selection when needed and in the background warmer
	 after indexing, never as a prerequisite for the first sidebar response.

The normalized summary owns the `_loaded_details` cache entry. The cache is
shared by the operational view and Statistics: a session selected in one view
must make its parsed details available to the other view without reparsing.
Statistics may expose only cached rows while the warmer progresses and must
load or aggregate incrementally rather than synchronously parsing every
session. Derived expensive values, such as model and granular effort usage
breakdowns, should be cached on the loaded details and reused across renders.

The background warmer processes one session at a time and must not replace the
sidebar DOM while full transcripts are loading. High-level indexing may update
sidebar rows progressively; full-detail warming updates only detail/statistics
content and its progress state. Preserve current filters, scroll positions,
controls, and event handlers.

### UI Loading Handling

Use the sidebar progress bars to communicate background work without blocking
the viewer:

- The **green light-loading bar** means provider indexes are still discovering
	high-level session summaries. It may update the sidebar rows progressively.
- The **red heavy-loading bar** means indexing is complete and the background
	worker is parsing full transcripts into the shared cache. Do not replace or
	refresh the sidebar session list during this phase; update only detail or
	Statistics content and the progress state.
- Statistics should open immediately with cached data, or an empty/lightweight
	shell when no details are ready, and fill progressively as the heavy cache is
	warmed. Group-by-model and group-by-effort views must reuse cached details
	and derived breakdowns.
- Preserve sidebar filter text, scroll position, expanded controls, click
	handlers, and the current detail scroll position during polling updates.
	Replace only the smallest required DOM regions.

The full-page loading popup is a short-lived navigation fallback, not the
normal background-scan indicator. Show it only while the viewer is loading a
requested view or selected session and no cached session details are available
to render immediately. Do not keep the popup visible while background warming
continues, and do not show it for cached session navigation; use the green or
red sidebar bar instead.

Cache invalidation rules are explicit:

- A complete sidebar refresh creates a new scan manager and invalidates all
	summaries, loaded details, and derived breakdown caches.
- The per-session refresh control removes only that session's `_loaded_details`
	and derived caches, then reloads it from provider storage.
- Deletion removes the session from every sidebar, statistics, and cache
	collection.
- Do not silently rescan or refresh cached sessions on ordinary navigation.
- If a provider source is unavailable or a detail parse fails, retain the
	lightweight summary and mark the detail attempt as completed without
	blocking other sessions.

## UI Token Card Hierarchy

Keep token cards visually smaller as their scope becomes more specific:

1. Session totals — largest cards.
2. Turn totals — smaller than session cards.
3. Invocation totals — smaller than turn cards.
4. Delegated-agent totals — smallest cards.

When changing token-card styles, preserve this order using the existing scope selectors: `.overview > .metrics`, `.turn-metrics`, `.invocation-total`/`.invocation-parent-body`, and `.delegated-agent`. The hierarchy applies to card dimensions, padding, and value typography while retaining readable labels and accessible contrast.
