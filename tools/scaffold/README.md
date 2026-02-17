# `tools/scaffold`

`tools/scaffold/scaffold.py` is a stdlib-only Python CLI for managing this monorepo.

## Golden path

List available kinds and generators, then scaffold a project and run its tasks:

    python tools/scaffold/scaffold.py doctor
    python tools/scaffold/scaffold.py kinds
    python tools/scaffold/scaffold.py generators
    python tools/scaffold/scaffold.py add app billing-api --generator python_stdlib_copy --no-install
    python tools/scaffold/scaffold.py run install --project billing-api --skip-missing
    python tools/scaffold/scaffold.py run test --project billing-api

## Minimal requirements

- Always required: `python` on PATH (Python 3.11+ recommended; older Pythons need `tomli` installed to parse TOML).
- Required only for Cookiecutter-based generators: `cookiecutter` on PATH.
-
- Required only for external Cookiecutter sources and vendoring: `git` on PATH.
- Required only for running tasks: whatever commands your `tasks.*` reference (e.g. `poetry`, `uv`, `npm`, `cargo`, `terraform`).

## Cookiecutter sources

- Local templates should be referenced as paths (for example `tools/templates/internal/my-template`).
- External templates must be git-accessible sources (for example `https://...` git URLs, `gh:org/repo` shorthands, or
  `file://...` URIs that point at a git repo). Non-git HTTP/zip sources are not supported by this tool.

## Virtual environments

This tool does not create or manage virtual environments. For Python projects, use whatever per-project environment
strategy your generator and `tasks.*` imply (Poetry/uv/pip-tools/conda/PDM/venv/etc.). The scaffolder runs tasks exactly
as recorded in `tools/scaffold/monorepo.toml`.

## Configuration model

- Kinds and generators are defined in `tools/scaffold/registry.toml`.
- Created projects are recorded in `tools/scaffold/monorepo.toml` and are the source of truth for repo-wide task execution
  and CI.
- When `kinds.<kind>.ci` enables `lint/test/build`, `scaffold add` requires the selected generator to define
  `tasks.lint/tasks.test/tasks.build` (override with `--allow-missing-ci-tasks`).

## Included generators (default registry)

The default `tools/scaffold/registry.toml` includes:

- `python_stdlib_copy` and `python_stdlib_cookiecutter` (stdlib-only Python skeletons)
- `python_pdm_lib` and `python_pdm_app` (PDM-based Python projects; requires `pdm` on PATH to run tasks)
- `python_poetry_app` (Poetry-based Python project; requires `poetry` on PATH to run tasks)
- `python_uv_app` (uv-based Python project; requires `uv` on PATH to run tasks)
- `node_vite` (Vite-based Node project; requires `npm` on PATH to run tasks)
- `terraform_module` (Terraform module skeleton)

Example:

    python tools/scaffold/scaffold.py add lib my-lib --generator python_pdm_lib --no-install

## Trust model (external templates)

Cookiecutter templates can execute code via hooks. Treat external templates as untrusted by default:

- Prefer pinning external templates to a git ref via `generators.<id>.ref`.
- If a generator is configured with `trusted = false`, `scaffold add` will refuse to run it unless you pass `--trust` for
  that run.
- For long-lived use, vendor the external template into this repo:

    python tools/scaffold/scaffold.py vendor import <generator_id> --as <vendored_id>

Vendoring copies the upstream template into `tools/templates/vendor/<vendored_id>`, writes an `UPSTREAM.toml` with the
pinned commit and license metadata, and appends a new generator entry to `tools/scaffold/registry.toml`.

## Generator types (registry examples)

Copy (local skeleton directory):

    [generators.terraform_module]
    type = "copy"
    source = "tools/templates/internal/terraform-module"
    toolchain = "terraform"
    package_manager = "none"
    tasks.lint = ["terraform", "fmt", "-check", "-recursive"]

Cookiecutter (local or external; external should be pinned and untrusted by default):

    [generators.external_cookiecutter_x]
    type = "cookiecutter"
    source = "https://github.com/someone/some-template.git"
    ref = "v1.2.3"
    trusted = false
    toolchain = "python"
    package_manager = "poetry"
    tasks.install = ["poetry", "install"]
    tasks.test = ["poetry", "run", "pytest"]

Command (anything that can create the destination directory):

Prefer `{dest_path}` (repo-relative) over `{dest_dir}` (absolute), because some third-party generators do not handle
absolute paths reliably.

    [generators.node_vite]
    type = "command"
    toolchain = "node"
    package_manager = "npm"
    command = ["npm", "create", "vite@latest", "{dest_path}"]
    tasks.install = ["npm", "install"]
    tasks.build = ["npm", "run", "build"]

## CI behavior (generated monorepo)

The repo-level workflow at `.github/workflows/ci.yml` is driven by the manifest:

- `tools/scaffold/ci_matrix.py` reads `tools/scaffold/monorepo.toml` and emits a GitHub Actions matrix.
- The CI job runs `scaffold.py doctor`, then `scaffold.py run install --skip-missing`, then runs lint/test/build per
  project based on each project's `ci` flags and recorded `tasks.*` commands.
- CI installs a pinned PDM version (`2.26.2`) for deterministic behavior.
- `scaffold.py run install` applies a bounded one-time retry only for a known transient PDM local-path resolution
  signature involving `normalized-events`.
- The `scaffold_golden_path_smoke` CI job exercises the documented scaffold flow end to end by running
  `doctor -> add -> run install --skip-missing -> run test` on a generated sample app and asserting expected files plus
  manifest entries.
