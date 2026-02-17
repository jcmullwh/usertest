# Normalized event model (MVP)

Each run emits `normalized_events.jsonl` where each line is a JSON object:

```json
{"ts":"2026-01-20T12:00:00Z","type":"read_file","data":{"path":"README.md","bytes":1234}}
```

## Types

- `read_file`: `path`, `bytes`
- `write_file`: `path`, `lines_added`, `lines_removed` (derived from workspace diffs, not tool logs)
- `run_command`: `command`, `exit_code`
- `web_search`: `query`
- `tool_call`: `name`, `input`, `is_error`
- `agent_message`: `kind` (`plan|observation|decision`), `text`
- `error`: `category`, `message`

## Notes

- Adapters should be permissive in what they accept from raw logs and conservative in what they emit.
- Anything unrecognized can be emitted as `error` with `category="unparsed_raw_event"`.
- `write_file` represents actual workspace changes. Attempted edits from agent tool calls should be recorded as `tool_call` events instead (they vary by agent and are not comparable).

## Golden fixtures and drift checks

Normalized event behavior is locked by checked-in fixtures under `examples/golden_runs/`:

- `minimal_codex_run`
- `minimal_claude_run`
- `minimal_gemini_run`

Regression tests compare each fixture's `raw_events.jsonl` against the expected
`normalized_events.jsonl`:

`python -m pytest -q packages/agent_adapters/tests/test_golden_normalization_fixtures.py`
