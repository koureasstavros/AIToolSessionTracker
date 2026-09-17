# xAI Cursor

## Local storage

Cursor agent transcripts are stored as JSONL files below:

```text
~/.cursor/projects/<project>/agent-transcripts/<session>/<session>.jsonl
```

On Windows this is normally `%USERPROFILE%\.cursor\projects`. Set
`XAI_CURSOR_ROOT` to override the projects directory when using a non-standard
xAI Cursor profile or an exported fixture. The legacy `CURSOR_ROOT` variable
is also accepted.

The viewer reads the local transcript only. It does not connect to Cursor or
xAI services.

## Token accounting

The current Cursor JSONL transcript format does not persist token usage. The
viewer therefore estimates tokens at approximately one token per four
characters from visible user and assistant text, plus serialized tool
arguments and results. Estimated values are marked as such in the session
view and must not be treated as billing data.

## Limitations

- Sessions with no user or assistant text are hidden from the normal sidebar.
- Model metadata is displayed when a future transcript includes it; otherwise
  the model remains unavailable.
- Deleting a session removes its local transcript directory and is irreversible.
