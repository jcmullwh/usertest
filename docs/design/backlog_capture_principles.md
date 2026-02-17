# Backlog and Analysis Capture Principles

This note documents the capture invariants for `reporter.backlog` and
`reporter.analysis`.

## Why this exists

Backlog mining quality depends on what evidence gets extracted into atoms/signals.
If extraction silently drops artifacts, the downstream miner can miss novel failure
modes and produce misleadingly confident tickets.

## Historical omission paths (now removed)

- `packages/reporter/src/reporter/backlog.py` previously used `_safe_read_text`
  with a hard size cap and returned `None` for large files.
- `packages/reporter/src/reporter/backlog.py` previously gated `agent_stderr` and
  `agent_last_message` inclusion behind `_INTERESTING_*` regexes.
- `packages/reporter/src/reporter/backlog.py` previously normalized all whitespace
  to a single line via `_normalize_space`, which destroyed structured evidence
  (stack traces, multiline diagnostics).
- `packages/reporter/src/reporter/analysis.py` repeated the same keyword gating and
  size-drop behavior for stderr and last-message signals.
- `apps/usertest/src/usertest/backlog_exec.py` previously selected orphan atoms
  from preview-only coverage fields, effectively capping orphan discovery.
- `apps/usertest/src/usertest/backlog_exec.py` previously allowed silent prompt
  fallbacks through embedded template defaults.

## Required invariants

- Existing text artifacts are never silently dropped.
- Artifact capture is loss-accounted through:
  - `artifact_ref` metadata on artifact atoms/signals.
  - `capture_manifest` entries with `exists`, `size_bytes`, `sha256`,
    `truncated`, and `error`.
- Truncation is explicit (head/tail excerpts plus `truncated=true`), never hidden.
- Prompt templates are manifest-driven (`configs/backlog_prompts/manifest.json`).
  Missing templates fail loudly with explicit errors.
- Orphan passes operate on full uncovered high-severity atom sets, not preview
  slices.
- CLI `--sample-size 0` means uncapped sampling and is preserved in output
  metadata.
