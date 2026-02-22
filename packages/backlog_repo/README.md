# `backlog_repo`

`backlog_repo` contains helpers for treating a repository as a **first-class backlog source**.

It is intentionally opinionated toward the “agentic-usertest” workflow, where backlog state is
tracked alongside code under `.agents/`.

This package provides:

- loading/writing **actions ledgers** (`backlog_actions.yaml`, `backlog_atom_actions.yaml`)
- mapping `.agents/plans/…` folder state into backlog atom status
- ticket export helpers (anchors + stable fingerprints)

It is used by `usertest-backlog` to keep backlog state consistent across runs and exports.

---

## Install

Distribution name: `backlog_repo`

### Standalone package checkout (recommended first path)

Run from this package directory:

```bash
pdm install
pdm run smoke
pdm run test
pdm run lint
```

If you need only a runtime install (without dev tooling commands), use:

```bash
python -m pip install -e .
```

From a private GitLab PyPI registry (if you publish it):

```bash
pip install \
  --index-url "https://<gitlab-host>/api/v4/projects/<project_id>/packages/pypi/simple" \
  --extra-index-url "https://pypi.org/simple" \
  "backlog_repo==<version>"
```

> Publishing note
>
> This package is currently treated as **internal** unless opted into snapshot publishing via
> `[tool.monorepo].status` in `pyproject.toml`. See `docs/monorepo-packages.md`.

---

## Canonical smoke

Run from this package directory:

```bash
pdm run smoke
pdm run smoke_extended
```

`pdm run smoke` is the deterministic first-success check. `pdm run smoke_extended` keeps a second
tier for broader validation passes.

---

## Public API

- actions ledger helpers:
  - `load_backlog_actions_yaml(...)`
  - `load_atom_actions_yaml(...)`
  - `write_atom_actions_yaml(...)`
  - `normalize_atom_status(...)`, `promote_atom_status(...)`
- plan folder indexing:
  - `scan_plan_ticket_index(...)`
  - `sync_atom_actions_from_plan_folders(...)`
- export helpers:
  - `ticket_export_anchors(...)`
  - `ticket_export_fingerprint(...)`

---

## How it fits in the system

`backlog_repo` connects “run-derived issues” to “repo-maintained backlog state”:

- `backlog_core` extracts atoms/tickets from run evidence.
- `backlog_repo` syncs that with `.agents/` planning/tracking state.
- `usertest-backlog` orchestrates and writes outputs.

---

## Development

### Standalone package checkout (recommended first path)

Run from this package directory:

```bash
pdm install
pdm run smoke
pdm run smoke_extended
pdm run test
pdm run lint
```

### Monorepo contributor workflow

Run from the monorepo root:

```bash
python tools/scaffold/scaffold.py run install --project backlog_repo
python tools/scaffold/scaffold.py run test --project backlog_repo
python tools/scaffold/scaffold.py run lint --project backlog_repo
```
