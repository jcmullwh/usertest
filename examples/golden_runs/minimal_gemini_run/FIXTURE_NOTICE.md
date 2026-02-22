# Fixture notice: minimal/synthetic run directory

This directory is a **synthetic**, **minimal**, **sanitized** fixture used for documentation and
regression tests. It is not a full `usertest run` output directory.

Compared to a normal successful run, this fixture intentionally omits many artifacts, including:

- `effective_run_spec.json` and persona/mission markdown (`persona.*`, `mission.*`)
- `preflight.json` and detailed command diagnostics
- `agent_attempts.json` and per-attempt artifacts (for example `raw_events.attempt1.jsonl`)
- `agent_stderr.txt` (present in real runs; may be empty)
- `sandbox/` diagnostics (docker container logs/inspect, DNS snapshots, etc.)
- failure-only artifacts like `error.json` / `report_validation_errors.json`

See `docs/design/run-artifacts.md` for the full run artifact contract.

