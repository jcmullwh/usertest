# Scripts

This folder contains small repo scripts.

They are convenience helpers; the “official” monorepo workflow is driven by `tools/scaffold/`.

Onboarding precedence:

1. **Canonical newcomer-first path:** `offline_first_success.ps1` / `offline_first_success.sh`
2. **Diagnostic alternate:** `doctor.ps1` / `doctor.sh`
3. **Secondary developer sanity check:** `smoke.ps1` / `smoke.sh`

---

## Doctor + smoke scripts

- `smoke.sh`
- `smoke.ps1`
- `doctor.sh`
- `doctor.ps1`
- `python_preflight.sh` (shared bash Python resolver)
- `python_preflight.ps1` (shared Windows Python resolver)
- `../tools/python_toolchain.py` (shared python/pip/pdm/venv contract used by the wrappers after interpreter selection)

These are the diagnostic / developer-sanity-check entrypoints that come after, or alongside, the
canonical newcomer-first path:

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

All of these scripts honor `USERTEST_PYTHON` first. When set, the scripts reuse that explicit
interpreter decision instead of re-resolving `python` / `python3` from PATH.

After selecting an interpreter, the wrappers call `tools/python_toolchain.py resolve` so setup,
doctor, smoke, and offline-first-success flows all share one diagnostic contract for:

- interpreter health
- `python -m pip` availability / `ensurepip` bootstrap
- usable `pdm`
- `.venv` creation (including temp fallback for offline-first-success)

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

### Canonical newcomer-first path (offline-safe)

- `offline_first_success.sh`
- `offline_first_success.ps1`

These scripts create/use a local `.venv`, install `requirements-dev.txt`, set `PYTHONPATH`, and
re-render a golden fixture report in a scratch directory.

They are the canonical newcomer-first onboarding path for this repo: a quick way to verify that the
repo can render reports *from source* without calling any agents or network services.

- Windows PowerShell: `powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\offline_first_success.ps1`
- macOS / Linux: `bash ./scripts/offline_first_success.sh`

### Backwards-compatible fixture rerender aliases

- `offline_fixture_rerender.sh`
- `offline_fixture_rerender.ps1`

These wrappers continue to point at `offline_first_success.*` for compatibility.

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
