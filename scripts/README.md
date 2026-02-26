# Scripts

This folder contains small repo scripts.

They are convenience helpers; the “official” monorepo workflow is driven by `tools/scaffold/`.

---

## Smoke scripts

- `smoke.sh`
- `smoke.ps1`
- `doctor.sh`
- `doctor.ps1`
- `python_preflight.ps1` (shared Windows Python resolver)

These run a deterministic checklist used in onboarding and CI verification:

- doctor
- install
- CLI help
- smoke tests

If `pdm` is not installed, the smoke scripts still run doctor in “tool checks skipped” mode
(`python tools/scaffold/scaffold.py doctor --skip-tool-checks`).

Both `doctor.sh` and `doctor.ps1` support passing through `--require-pip` / `-RequirePip`.

Use strict preflight mode when needed:

- `smoke.sh --require-doctor`
- `smoke.ps1 -RequireDoctor`

In strict mode, missing `pdm` is treated as a failure instead of a skip.

### PowerShell parse preflight

To validate that a `.ps1` file parses cleanly (and to print line/column diagnostics on failure):

- `powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\parse_preflight.ps1 .\scripts\smoke.ps1`

See the repo root `README.md` for copy/paste invocations.

---

## Repo snapshot script

- `snapshot_repo.sh`
- `snapshot_repo.ps1`

These are thin wrappers around `tools/snapshot_repo.py`.

With no args, they default to writing `repo_snapshot.zip`. To preview without writing an archive:

- PowerShell: `powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\snapshot_repo.ps1 -DryRun`
- bash: `bash ./scripts/snapshot_repo.sh --dry-run`

---

## PYTHONPATH helpers

- `set_pythonpath.sh`
- `set_pythonpath.ps1`

These configure `PYTHONPATH` so you can run CLIs from source without editable installs.

### One-command fixture rerender (offline-safe)

- `offline_fixture_rerender.sh`
- `offline_fixture_rerender.ps1`

These scripts create/use a local `.venv`, install `requirements-dev.txt`, set `PYTHONPATH`, and
re-render a golden fixture report in a scratch directory.

They are a quick way to verify that the repo can render reports *from source* without calling any
agents. They do **not** validate agent quality or end-to-end runtime behavior.

### From-source pytest example

In an activated virtual environment:

`python -m pip install -r requirements-dev.txt`

Then run a minimal smoke test from source:

- PowerShell:
  - `. .\scripts\set_pythonpath.ps1`
  - `python -m pytest -q apps/usertest/tests/test_smoke.py`
- bash:
  - `source scripts/set_pythonpath.sh`
  - `python -m pytest -q apps/usertest/tests/test_smoke.py`

---

## Operational helpers

- `run_iteration_cycle.py`
- `render_operational_feedback.py`

These are used in internal workflows to iterate on runs and summarize feedback.
