# Cognition Devin

## Local storage

Devin CLI sessions are stored in a local SQLite database:

```text
%APPDATA%\Devin\cli\sessions.db
```

The provider also checks `~/.config/devin/cli/sessions.db` on systems without
`APPDATA`. Set `COGNITION_DEVIN_DB` to use another database path. The legacy
`DEVIN_DB` variable is also accepted.

The viewer reads the database locally and does not connect to Cognition or
Devin services.

## Token accounting

Devin assistant message metadata can include persisted usage metrics such as
`input_tokens`, `output_tokens`, `cache_read_tokens`, and
`cache_creation_tokens`. These values are used directly when present. The
viewer estimates missing values from visible content and marks the session as
estimated when that fallback is needed.

## Limitations

- Hidden sessions are excluded from the normal sidebar.
- The database may contain duplicate message nodes; duplicate message IDs are
  counted once.
- Deleting a session removes its related rows from `sessions.db` and is
  irreversible.
