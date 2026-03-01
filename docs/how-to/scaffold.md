# How to use the scaffold tool

`tools/scaffold/scaffold.py` is a stdlib-only Python CLI that manages this monorepo.

It is used to:

- create new projects (apps/packages) from templates
- record them in the monorepo manifest
- run tasks across projects (install/lint/test/build)

CI uses the manifest to generate its job matrix.

For the full reference (generators, trust model, vendoring), see `tools/scaffold/README.md`.

---

## Validate your environment

- **Windows PowerShell:** `powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\doctor.ps1`
- **macOS / Linux:** `bash ./scripts/doctor.sh`
- **Direct (any OS):** `python tools/scaffold/scaffold.py doctor`

This checks Python + temp directory health, reports whether `python -m pip` works, and checks required tools for recorded projects.
If you want doctor to fail when `pip` is missing, you can run:

- **Windows PowerShell:** `powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\doctor.ps1 -RequirePip`
- **macOS / Linux:** `bash ./scripts/doctor.sh --require-pip`
- **Direct (any OS):** `python tools/scaffold/scaffold.py doctor --require-pip`

---

## Add a new project

List kinds and generators:

```bash
python tools/scaffold/scaffold.py kinds
python tools/scaffold/scaffold.py generators
```

Create a new project (example: a PDM library under `packages/`):

```bash
python tools/scaffold/scaffold.py add lib my-lib --generator python_pdm_lib
```

This:

1) creates `packages/my-lib/…`
2) adds an entry to `tools/scaffold/monorepo.toml` (project id, path, tasks, CI flags)

> Project IDs matter
>
> The manifest `id` is what you pass to `scaffold run --project <id>` and what CI uses.

---

## Run tasks for a project

```bash
python tools/scaffold/scaffold.py run install --project my-lib
python tools/scaffold/scaffold.py run test --project my-lib
python tools/scaffold/scaffold.py run lint --project my-lib
```

Run a task across all projects (skipping those without that task):

```bash
python tools/scaffold/scaffold.py run test --all --skip-missing
```

---

## Common pitfalls

- **The app `usertest` is project id `cli`** (see `tools/scaffold/monorepo.toml`).
- If `scaffold run lint` fails with a message about missing `ruff`, run installs first (for example: `python tools/scaffold/scaffold.py run install --all`).
- If you add a project but want to defer installs, use `--no-install`.
- Some generators require external tools (Cookiecutter, Node, Terraform, …).
