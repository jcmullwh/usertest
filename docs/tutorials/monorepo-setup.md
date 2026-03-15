# Monorepo setup and workflow

This repository is intentionally an **unusual** Python monorepo:

- There are **multiple independent Python projects** under `apps/` and `packages/`.
- Each project has its own `pyproject.toml` and can be installed/published independently.
- CI and “run tasks across the repo” are driven by a repo-local tool: `tools/scaffold/scaffold.py`.

The goal is to allow fast iteration, experimentation, and evolution of a project structure without being locked into a particular opinion on packaging, versioning, or project layout.

This tutorial explains the mental model and shows the supported setup paths.

---

## Monorepo mental model

### Apps

`apps/` contains **end-user facing** deliverables (CLIs):

- `apps/usertest` → the `usertest` CLI (run usertests)
- `apps/usertest_backlog` → the `usertest-backlog` CLI (compile/analyze/export backlog)

Apps depend on the packages under `packages/`.

### Packages

`packages/` contains **reusable libraries**.

They are intended to be:

- usable from inside the monorepo (editable installs / local paths)
- publishable to an internal registry (snapshot builds today)
- consumable from *other* repositories

Snapshot publishing is implemented by:

- `tools/monorepo_publish/` (publisher implementation)
- `.github/workflows/publish-snapshots.yml` (the CI hook)

See `docs/monorepo-packages.md`.

### Tools

`tools/` contains repo utilities:

- `tools/scaffold/`: monorepo manager (manifest + generators + task runner)
- `tools/monorepo_publish/`: snapshot publisher for packages
- `tools/migrations/`: migrations for run layouts and other data
- various lint helpers

These are “internal tooling” — they may not be published as packages.

---

## Why the scaffold tool exists

`tools/scaffold/scaffold.py` is the **source of truth** for “what projects exist” and “how to run
their tasks” in this monorepo.

It reads:

- `tools/scaffold/registry.toml` – what generators exist (templates)
- `tools/scaffold/monorepo.toml` – the monorepo manifest (projects + their task commands)

CI uses it to generate the job matrix (`tools/scaffold/ci_matrix.py`).

This keeps the repo consistent even though each project can use different toolchains (PDM, Poetry,
uv, Node, Terraform, …).

---

## Setup paths

For a fresh repo checkout, the canonical entrypoint is the repo script layer:

- `scripts/doctor.*` to resolve a usable Python and check the environment
- `scripts/smoke.*` for contributor bootstrap + smoke
- `scripts/offline_first_success.*` for an offline-safe “first success”

Do **not** make `py`, direct `pdm`, explicit interpreter paths, or ad hoc `.venv` creation your
normal first step. Keep those as advanced/manual escape hatches after the script-backed path works.

You can work with this repo in a few ways. Pick the one that matches your goals.

### Option A: Repo scripts + scaffold (recommended for maintainers)

This matches what CI does conceptually and is the best way to run lint/test across multiple
projects, but the first-use entrypoint should be the repo wrappers because they resolve a usable
Python before any install/test command runs.

1) Ensure you have:

- Python 3.11+
- `git`
- optional: `pdm` if you later choose to run project-local PDM commands directly

2) Run the doctor:

- Windows PowerShell: `powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\doctor.ps1`
- macOS / Linux: `bash ./scripts/doctor.sh`
- Advanced/direct (bypasses the wrapper's Python resolver): `python tools/scaffold/scaffold.py doctor`

3) Run the canonical contributor bootstrap:

- Windows: `powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\smoke.ps1`
- macOS/Linux: `bash ./scripts/smoke.sh`

This path resolves Python first, fails fast when the interpreter selection is broken, installs the
repo requirements, bootstraps the local editables by default, and runs the smoke suite.

4) After the bootstrap succeeds, use scaffold for project-level tasks (example: the `usertest` CLI
project id is `cli`):

```bash
python tools/scaffold/scaffold.py run install --project cli
python tools/scaffold/scaffold.py run test --project cli
```

5) Run tasks across all projects (skip those without the task):

```bash
python tools/scaffold/scaffold.py run lint --all --skip-missing
python tools/scaffold/scaffold.py run test --all --skip-missing
```

> Where do project IDs come from?
>
> `tools/scaffold/monorepo.toml` is the source of truth.

If `scaffold run lint|test ...` later discovers missing host prerequisites, scaffold can bootstrap
them from `requirements-dev.txt` and inject repo `src/` paths into `PYTHONPATH` automatically.

### If the resolver rejects your environment

If `doctor`, `smoke`, or `offline_first_success` prints `No usable Python interpreter found` or
rejects candidates with reason codes like `windowsapps_alias`, `missing_stdlib`, `access_denied`,
`context_mismatch`, or `not_found`, stop there and fix the interpreter/backend first.

That early stop is intentional: it tells you the environment/toolchain is broken before `pip`,
`pdm`, scaffold, or pytest get involved. If you already know the correct interpreter, point the
wrappers at it with `USERTEST_PYTHON` and rerun the same wrapper.

### Option B: Manual app-only install (advanced)

If you only need `usertest` locally and intentionally want a manual install path, you can do a
normal editable install:

```bash
python -m pip install -r requirements-dev.txt
python -m pip install -e apps/usertest
python -m usertest.cli --help
# If you installed the console script: usertest --help
```

This is simple, but it is not the recommended first-use path for a fresh checkout because it
bypasses the script-backed interpreter resolver.

### Option C: Source-run via PYTHONPATH (advanced fallback)

If you intentionally don’t want editable installs, you can run from source using the helper scripts:

- Windows PowerShell: `. .\scripts\set_pythonpath.ps1`
- macOS/Linux: `source scripts/set_pythonpath.sh`

If you forget this step, the CLIs will fail fast with a short `Missing import ...` hint pointing back at these scripts.

---

## The smoke scripts

For the most copy/paste-friendly sanity check, use the OS-specific smoke script:

- Windows: `powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\smoke.ps1`
- macOS/Linux: `bash ./scripts/smoke.sh`

These run a small, deterministic checklist (doctor → install → CLI help → smoke tests) and resolve
Python through `scripts/python_preflight.*` before they touch installs.

---

## Next steps

- If you want to add a new package/app to this monorepo, see `docs/how-to/scaffold.md`.
- If you want to publish snapshot packages, see `docs/how-to/publish-snapshots.md`.
