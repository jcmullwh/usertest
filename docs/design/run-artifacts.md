# Run Artifact Contract

This document defines the stable artifact layout for a single `usertest run` output directory.
It is intended for operators, CI maintainers, and reviewers who need to inspect outputs without
reading runner source code.

## Run directory shape

Canonical run path:

`runs/usertest/<target_slug>/<timestamp_utc>/<agent>/<seed>/`

Example:

`runs/usertest/project_scaffold/20260214T201500Z/codex/0/`

## Redacted sample tree

This is a redacted, representative tree showing common files and optional files.

```text
runs/usertest/project_scaffold/20260214T201500Z/codex/0/
  target_ref.json
  effective_run_spec.json
  prompt.txt
  prompt.template.md
  report.schema.json
  persona.source.md
  persona.resolved.md
  mission.source.md
  mission.resolved.md
  preflight.json
  raw_events.jsonl
  normalized_events.jsonl
  metrics.json
  report.md
  report.json
  agent_last_message.txt
  agent_stderr.txt
  agent_attempts.json
  error.json                          # only when run fails
  report_validation_errors.json       # only when schema validation fails
  patch.diff                          # only when write policy allows edits and edits occurred
  diff_numstat.json                   # only when write policy allows edits and edits occurred
  preprocess_commit.txt               # only when workspace preprocess created a commit
  users.md                            # only when target USERS.md existed
  agent_prompts/                      # only when system prompt override/append files are staged
    system_prompt.md
    append_system_prompt.md
  sandbox/                            # only for docker backend
    sandbox.json
    docker_logs.txt
    docker_inspect.json
    dns_snapshot.txt
```

Offline reference fixture:

- `examples/golden_runs/minimal_codex_run/` provides a minimal sanitized artifact set used in tests.

## File-level contract

Files that are expected for successful runs in normal operation:

- `target_ref.json`: normalized target metadata (`repo_input`, git/ref context, target slug).
- `effective_run_spec.json`: resolved persona/mission/template/schema identifiers and paths.
- `prompt.txt`: final prompt sent to the agent.
- `prompt.template.md`: resolved prompt template source used to build `prompt.txt`.
- `report.schema.json`: schema snapshot used for report validation.
- `raw_events.jsonl`: raw adapter event stream.
- `normalized_events.jsonl`: normalized cross-agent event stream.
- `metrics.json`: computed metrics from normalized events.
- `report.md`: rendered markdown report (written even when `report.json` is absent).
- `agent_last_message.txt`: last agent textual output.
- `agent_stderr.txt`: stderr captured from adapter process (may be synthesized on non-zero exit).
- `agent_attempts.json`: per-attempt metadata for retries/follow-ups.

Files that are conditionally present:

- `report.json`: present when agent output parsed into a JSON object.
- `report_validation_errors.json`: present when report parsing/validation produced errors.
- `error.json`: present when run failed (preflight, adapter execution, or other fatal error path).
- `preflight.json`: present when preflight phase executed (normal path before adapter run).
- `patch.diff` and `diff_numstat.json`: present only when edits are allowed and edits occurred.
- `persona.source.md`, `persona.resolved.md`, `mission.source.md`, `mission.resolved.md`:
  present when catalog resolution succeeds.
- `users.md`: snapshot of target `USERS.md` when present.
- `preprocess_commit.txt`: present when preprocess logic writes and commits workspace changes.
- `agent_prompts/*`: present when prompt override/append files are staged.
- `sandbox/*`: present when using docker execution backend.

## Semantics and stability notes

- Filenames above are stable interface names for operators and tooling.
- Optional files should be treated as feature/condition indicators, not contract breakages.
- JSON files are UTF-8 encoded with pretty-printed payloads.
- Text/markdown artifacts are UTF-8 encoded.
- For failure triage, inspect in this order:
  - `error.json`
  - `agent_stderr.txt`
  - `agent_last_message.txt`
  - `preflight.json`
  - `agent_attempts.json`

## Verification commands

From repository root:

```powershell
rg -n "target_ref.json|effective_run_spec.json|report.schema.json|report_validation_errors.json" README.md docs/design
python -m pytest -q apps/usertest/tests/test_report_command.py apps/usertest/tests/test_golden_fixture.py
```
