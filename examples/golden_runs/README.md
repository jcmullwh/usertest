# Golden run fixtures

This folder contains small, synthetic run directories that demonstrate the run artifact contract
and support regression tests.

Available fixtures:

- `minimal_codex_run`
- `minimal_claude_run`
- `minimal_gemini_run`

Notes:

- Fixtures are **not** real agent runs; they are minimal/sanitized samples (no secrets, no
  personal paths).
- Fixtures intentionally omit many artifacts that exist in real `usertest run` directories. See
  `docs/design/run-artifacts.md` for the full contract and each fixture’s `FIXTURE_NOTICE.md` for
  a short omission list.
- `usertest report` can render `report.md` for any fixture directory.
- `usertest report --recompute-metrics` can re-normalize `raw_events.jsonl` into
  `normalized_events.jsonl` and regenerate `metrics.json`.
  - Note: `--recompute-metrics` overwrites `normalized_events.jsonl`; when an existing
    `normalized_events.jsonl` is present, its timestamps are reused when possible to keep reruns
    reproducible.

Example (from repository root):

`python -m usertest.cli report --repo-root . --run-dir examples/golden_runs/minimal_codex_run --recompute-metrics`
