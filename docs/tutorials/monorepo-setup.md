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

You can work with this repo in a few ways. Pick the one that matches your goals.

### Option A: “Monorepo-native” (recommended for maintainers)

This matches what CI does and is the best way to run lint/test across multiple projects.

1) Ensure you have:

- Python 3.11+
- `pdm` installed (CI pins a specific version; you can use any recent version locally)

2) Run the doctor:

```bash
python tools/scaffold/scaffold.py doctor
```

3) Install and test a project (example: the `usertest` CLI project id is `cli`):

```bash
python tools/scaffold/scaffold.py run install --project cli
python tools/scaffold/scaffold.py run test --project cli
```

4) Run tasks across all projects (skip those without the task):

```bash
python tools/scaffold/scaffold.py run lint --all --skip-missing
python tools/scaffold/scaffold.py run test --all --skip-missing
```

> Where do project IDs come from?
>
> `tools/scaffold/monorepo.toml` is the source of truth.

### Option B: “I just want to run the CLI” (quickest)

If you only need `usertest` locally, you can do a normal editable install:

```bash
python -m pip install -r requirements-dev.txt
python -m pip install -e apps/usertest
usertest --help
```

This path is simple, but you lose the “run tasks across the whole monorepo” ergonomics.

### Option C: Source-run via PYTHONPATH (fallback)

If you intentionally don’t want editable installs, you can run from source using the helper scripts:

- Windows PowerShell: `. .\scripts\set_pythonpath.ps1`
- macOS/Linux: `source scripts/set_pythonpath.sh`

---

## The smoke scripts

For the most copy/paste-friendly sanity check, use the OS-specific smoke script:

- Windows: `powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\smoke.ps1`
- macOS/Linux: `bash ./scripts/smoke.sh`

These run a small, deterministic checklist (doctor → install → CLI help → smoke tests).

---

## Next steps

- If you want to add a new package/app to this monorepo, see `docs/how-to/scaffold.md`.
- If you want to publish snapshot packages, see `docs/how-to/publish-snapshots.md`.
