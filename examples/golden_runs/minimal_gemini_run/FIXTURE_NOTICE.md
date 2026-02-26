# Fixture notice: minimal/synthetic run directory

This directory is a **synthetic**, **minimal**, **sanitized** fixture used for documentation and
regression tests. It is not a full `usertest run` output directory.

Compared to a normal successful run, this fixture is still intentionally small and sanitized.

It includes lightweight stubs for common "successful run" artifacts (for example
`effective_run_spec.json`, `prompt.template.md`, `agent_attempts.json`, `agent_stderr.txt`,
`run_meta.json`, and `verification.json`) but intentionally omits many other artifacts, including:

- `preflight.json` and detailed command diagnostics
- per-attempt artifacts (for example `raw_events.attempt1.jsonl`)
- `sandbox/` diagnostics (docker container logs/inspect, DNS snapshots, etc.)
- failure-only artifacts like `error.json` / `report_validation_errors.json`

See `docs/design/run-artifacts.md` for the full run artifact contract.
