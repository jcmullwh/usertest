# `backlog_core`

`backlog_core` is a Python library for turning many usertest runs into a **backlog-oriented view of
issues**.

It provides:

- extraction of **backlog atoms** from run artifacts (`report.json`, `error.json`, stderr, etc.)
- ticket **deduping** and candidate merge suggestions
- **coverage** computation across runs (what issues recur, what’s new)
- rendering helpers for backlog documents
- a policy layer to apply repo-specific decision rules

It is used heavily by the `usertest-backlog` CLI, but can be consumed as a standalone library.

---

## Install

Distribution name: `backlog_core`

### Standalone package checkout (recommended first path)

Run from this package directory:

```bash
pdm install
pdm run smoke
pdm run test
pdm run lint
```

Dependencies for standalone use:
- `backlog_core` imports `run_artifacts` and `triage_engine` at runtime.
- If your package index does not provide those internal packages, install local checkouts first.
- From a sibling checkout layout, run:

```bash
python -m pip install -e ../run_artifacts -e ../triage_engine
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
  "backlog_core==<version>"
```

> Publishing note
>
> This package is currently treated as **internal** unless opted into snapshot publishing via
> `[tool.monorepo].status` in `pyproject.toml`. See `docs/monorepo-packages.md`.

---

## Quickstart

Extract atoms from a compiled report history (JSON objects) and render a simple markdown backlog:

```python
from backlog_core import extract_backlog_atoms, render_backlog_markdown

# records is typically loaded from a report-history JSONL file.
records = [
    {
        "run_dir": "runs/usertest/my-target/20260216T010203Z/codex/seed0",
        "status": "ok",
        "report": {"backlog": []},
    }
]

atoms_doc = extract_backlog_atoms(records)
markdown = render_backlog_markdown(atoms_doc)
print(markdown)
```

---

## Public API

Most consumers only need the top-level exports:

- `extract_backlog_atoms(records, ...)`
- `dedupe_tickets(tickets, ...)`
- `build_merge_candidates(tickets, ...)`
- `compute_backlog_coverage(atoms, ...)`
- `build_backlog_document(...)`
- `render_backlog_markdown(document)`
- `write_backlog(path, document)`
- `BacklogPolicyConfig`, `apply_backlog_policy(...)`

Source modules:

- `backlog_core.backlog`
- `backlog_core.backlog_policy`

---

## How it fits in the system

`backlog_core` sits “after the run”:

1) `runner_core` produces per-run artifacts.
2) `reporter` validates and renders reports.
3) `backlog_core` extracts atoms and builds backlog documents.

Design rationale for strict capture invariants:

- `docs/design/backlog_capture_principles.md`

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
python tools/scaffold/scaffold.py run install --project backlog_core
python tools/scaffold/scaffold.py run test --project backlog_core
python tools/scaffold/scaffold.py run lint --project backlog_core
```
