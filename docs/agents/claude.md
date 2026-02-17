# Claude Code adapter notes

Implemented in MVP (headless `claude -p`).

Notes:

- Use `claude -p` headless mode with `--output-format json|stream-json`.
- The adapter defaults to `stream-json` so it can normalize tool usage into `normalized_events.jsonl`.
- When using `--output-format stream-json`, Claude Code requires `--verbose` in print mode (the adapter adds this automatically).
- `--policy inspect` is the recommended read-only mode for Claude runs (it enables `Bash` for lightweight repo inspection while still disallowing edits). `--policy safe` is stricter and disables shell commands.
- The final report is validated client-side against the mission-selected schema (snapshotted as `report.schema.json` in each run directory).
- Windows tip: if agent-authored `bash` invocations are noisy, prefer `--exec-backend docker` for
  POSIX shell behavior, or run with `--policy safe` when shell commands are not required.
- Cosmetic vs blocking on Windows:
  - Cosmetic: run exits `0` and report artifacts are produced, but `raw_events.jsonl` contains a
    few failed shell commands.
  - Blocking: preflight fails (`error.json` subtype `policy_block` / `mission_requires_shell`) or
    agent execution exits non-zero.
