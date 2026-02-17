# Gemini CLI adapter notes

Implemented in MVP (headless `gemini --output-format stream-json`).

Notes:

- The adapter uses `--output-format stream-json` so tool calls can be normalized into `normalized_events.jsonl`.
- Policies are expressed via `--sandbox`, `--approval-mode`, and `--allowed-tools` (see `configs/policies.yaml`).
- `--policy inspect` is the recommended read-only mode for Gemini runs (it enables `run_shell_command` for lightweight repo inspection while still disallowing edits). `--policy safe` is stricter and disables shell commands.
- The final report is validated client-side against the mission-selected schema (snapshotted as `report.schema.json` in each run directory).
- Gemini CLI requires Node.js 20+. Node 18 can crash early with errors like `SyntaxError: Invalid regular expression flags` (often referencing `/v`). If you hit this, upgrade Node or run Gemini via the runner's Docker backend (`--exec-backend docker`), which prefers the NodeSource Node 20 LTS repo when `nodejs` is installed in the sandbox image.
- Windows tip: for reliable shell behavior use `--exec-backend docker` (recommended for
  `--policy inspect`), since local nested Gemini sandboxing can be unavailable on Windows hosts.
- Cosmetic vs blocking on Windows:
  - Cosmetic: run exits `0` and report artifacts are produced, but `raw_events.jsonl` contains a
    few failed shell commands.
  - Blocking: preflight fails (`error.json` subtype `policy_block` / `mission_requires_shell`) or
    agent execution exits non-zero.
