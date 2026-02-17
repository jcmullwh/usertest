# Codex CLI adapter notes (MVP)

This repo's MVP implements Codex via `codex exec` in headless mode.

## Invocation (conceptual)

- Capture tool events via `--json` (JSONL on stdout).
- Capture final assistant message via `--output-last-message <file>`.
- Run inside the target workspace via `--cd <dir>`.
- Constrain writes via `--sandbox read-only` (safe) or `--sandbox workspace-write` (explicit).

The runner validates the final JSON report client-side against the mission-selected schema (snapshotted as `report.schema.json` in each run directory).

## Known limitation: `apply_patch_approval_request` can block headless runs

The Codex CLI can emit `apply_patch_approval_request` when it wants to write files via its internal patch tool.
In headless mode, the runner cannot provide interactive approval, so the process would otherwise hang.

To keep runs deterministic, the adapter terminates Codex if it emits `apply_patch_approval_request` and records
an error in `agent_stderr.txt` / `error.json`.

Workarounds:

- Use `--policy safe` / `--policy inspect` for read-only runs.
- Use `--agent claude` / `--agent gemini` for edit-capable runs.
