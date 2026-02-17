# `usertest-backlog` CLI

`usertest-backlog` is the backlog-focused companion CLI.

Use it when you already have usertest runs and you want to:

- compile run directories into a history file
- analyze outcomes across many runs
- build/review backlog documents
- export tickets
- triage PRs

If you’re looking to *run* usertests, use `usertest` instead.

---

## Install

From the monorepo root (editable install):

```bash
python -m pip install -r requirements-dev.txt
python -m pip install -e apps/usertest_backlog
```

Confirm:

```bash
usertest-backlog --help
```

---

## Core commands

### Reports workflows

Commands are grouped under `reports`:

- `compile`: scan run directories and write a JSONL history file
- `analyze`: analyze a history file and write an issue analysis summary
- `review-ux`: UX-focused review of reports
- `export-tickets`: export tickets (format depends on repo config)
- `backlog`: render backlog documents

Example:

```bash
usertest-backlog reports compile --repo-root . --runs-dir runs/usertest --out runs/usertest/report_history.jsonl
usertest-backlog reports analyze --repo-root . --history runs/usertest/report_history.jsonl
```

### PR triage

```bash
usertest-backlog triage-prs --repo-root . --repo "PATH_OR_GIT_URL"
```

---

## Configuration

This CLI relies on:

- run artifact contract (`docs/design/run-artifacts.md`)
- backlog policy and prompt manifests under `configs/`
- repo-local tracking under `.agents/` (plans, todos, actions ledgers)

Operational notes (ticket export workflow, remediation plans) live under `.agents/ops/`.

---

## Development

From the repo root:

```bash
python tools/scaffold/scaffold.py run install --project usertest_backlog
python tools/scaffold/scaffold.py run test --project usertest_backlog
python tools/scaffold/scaffold.py run lint --project usertest_backlog
```

Smoke tests:

```bash
python -m pytest -q apps/usertest_backlog/tests/test_smoke.py
```
