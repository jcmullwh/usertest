# Tools

This folder contains **internal repo tooling**.

These tools are not the “product surface” of the repo (the product is the `usertest` and
`usertest-backlog` CLIs), but they keep the monorepo consistent and operable.

---

## Monorepo management

### `tools/scaffold/`

The monorepo manager:

- owns the project manifest (`tools/scaffold/monorepo.toml`)
- runs tasks across projects (`scaffold run …`)
- scaffolds new projects from templates (`scaffold add …`)
- generates the CI matrix (`tools/scaffold/ci_matrix.py`)

Docs: `tools/scaffold/README.md`

---

## Snapshot publishing

### `tools/monorepo_publish/`

Publishes snapshot builds of selected packages to a private GitLab PyPI registry.

Docs:

- `docs/monorepo-packages.md`
- `docs/how-to/publish-snapshots.md`

---

## Migrations

### `tools/migrations/`

One-off migrations for run layouts and other on-disk formats.

Docs: `tools/migrations/README.md`

---

## Templates

### `tools/templates/`

Project templates used by the scaffold tool.

Docs: `tools/templates/README.md`

---

## Lint helpers

- `lint_prompts.py` – validate prompt/template manifests
- `lint_local_dependency_urls.py` – block accidental `file://` path deps leaking into published builds
- `lint_analysis_principles.py` – guardrail checks for analysis output / invariants

These are typically invoked by CI.

---

## Other utilities

- `snapshot_repo.py` – create a shareable snapshot ZIP of this repo
  - Minimal: `python tools/snapshot_repo.py --out repo_snapshot.zip`
  - Preview (no archive written): `python tools/snapshot_repo.py --dry-run`
  - Key defaults:
    - `.gitignore` files are excluded by default; pass `--include-gitignore-files` to include them.
    - Untracked-but-not-ignored files are included by default; pass `--tracked-only` to exclude them.
  - Common pitfalls:
    - If the output already exists, pass `--overwrite`.
    - `--no-verify` skips post-write invariant checks; only use it if you trust the tool (or when debugging performance).
  - Audit helpers:
    - `python tools/snapshot_repo.py --list-included`
    - `python tools/snapshot_repo.py --list-excluded --list-limit 200` (prints `PATH<TAB>REASON`)
- `pdm_shim.py` – small shim to make PDM invocation consistent across environments
