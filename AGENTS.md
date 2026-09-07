# Repository Guidelines

## Project Structure & Module Organization

This repository is a local, read-only Python web app for exploring AI coding-agent sessions. The main entry point is `session_token_viewer.py`. Provider-specific discovery, parsing, deletion, and archive logic belongs in `src/providers/`; keep provider-neutral behavior in the main application. Tests are in `tests/`, provider behavior and storage notes are in `docs/`, and screenshots or other visual assets are in `material/`. `.vscode/` and `.claude/` contain local development configuration.

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
