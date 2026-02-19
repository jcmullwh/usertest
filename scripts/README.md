# Scripts

This folder contains small repo scripts.

They are convenience helpers; the “official” monorepo workflow is driven by `tools/scaffold/`.

---

## Smoke scripts

- `smoke.sh`
- `smoke.ps1`

These run a deterministic checklist used in onboarding and CI verification:

- doctor
- install
- CLI help
- smoke tests

If `pdm` is not installed, the smoke scripts still run doctor in “tool checks skipped” mode
(`python tools/scaffold/scaffold.py doctor --skip-tool-checks`).

Use strict preflight mode when needed:

- `smoke.sh --require-doctor`
- `smoke.ps1 -RequireDoctor`

In strict mode, missing `pdm` is treated as a failure instead of a skip.

See the repo root `README.md` for copy/paste invocations.

---

## PYTHONPATH helpers

- `set_pythonpath.sh`
- `set_pythonpath.ps1`

These configure `PYTHONPATH` so you can run CLIs from source without editable installs.

---

## Operational helpers

- `run_iteration_cycle.py`
- `render_operational_feedback.py`

These are used in internal workflows to iterate on runs and summarize feedback.
