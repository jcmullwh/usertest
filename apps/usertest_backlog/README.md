# `usertest-backlog` CLI

`usertest-backlog` is the backlog-focused companion CLI.

Use it when you already have usertest runs and you want to:

- compile run directories into a history file
- analyze outcomes across many runs
- build/review backlog documents
- export tickets
- triage PRs
- triage issue backlogs into dedupe + theme clusters

If you're looking to *run* usertests, use `usertest` instead.

---

## Install

This repo uses `pdm` (do not use `pip` / `python -m ...` directly).

From the monorepo root:

```bash
cd apps/usertest_backlog
pdm install -d
```

Confirm:

```bash
pdm run usertest-backlog --help
```

---

## Core commands

### Reports workflows

Commands are grouped under `reports`:

- `compile`: scan run directories and write a JSONL history file
- `analyze`: analyze a history file and write an issue analysis summary
- `window`: summarize the last N runs vs previous N runs (timing + outcomes + regressions)
- `review-ux`: UX-focused review of reports
- `export-tickets`: export tickets (format depends on repo config)
- `backlog`: render backlog documents

Example:

```bash
usertest-backlog reports compile --repo-root . --runs-dir runs/usertest --out runs/usertest/report_history.jsonl
usertest-backlog reports analyze --repo-root . --history runs/usertest/report_history.jsonl
usertest-backlog reports window --repo-root . --runs-dir runs/usertest --last 12 --baseline 12
```

Notes:

- `reports analyze --history <path.jsonl>` reads the compiled JSONL directly.
- When `--history` is used and `--out-json/--out-md` are omitted, outputs are written next
  to the history file as `<stem>.issue_analysis.json` and `<stem>.issue_analysis.md`.
- `reports backlog` runs the **six-stage backlog pipeline** and writes inspectable stage artifacts
  alongside the final backlog:

  - `*.problem_records.json` / `*.problem_records.md`
  - `*.prioritized_problems.json` / `*.prioritized_problems.md`
  - `*.research.json` / `*.research.md`
  - `*.solution_options.json` / `*.solution_options.md`
  - `*.solution_selection.json` / `*.solution_selection.md`
  - `*.change_plans.json` / `*.change_plans.md`
  - `*.backlog.json` / `*.backlog.md`

  `review-ux` is driven by stage 5 (`*.solution_selection.json`) rather than early-stage tickets.
- `reports backlog --dry-run` is offline: it does not invoke an agent. It synthesizes deterministic
  stage outputs so fixtures/tests can validate the full chain end-to-end.
- Backlog mining knobs from the legacy one-pass miner (`--miners`, `--coverage-miners`,
  `--bagging-miners`, `--orphan-pass`, `--labelers`, merge flags) are still accepted but are
  ignored by the six-stage pipeline (the CLI prints a note when they are non-default).
- `reports backlog` excludes atoms with prior outcomes by default (`ticketed` + `queued` + `actioned`).
  To regenerate the backlog while keeping only actioned work excluded, use
  `--carryover-actioned-only` (demotes `ticketed`/`queued` atoms back to `new` before filtering).

### PR triage

```bash
usertest-backlog triage-prs --in apps/usertest_backlog/tests/fixtures/pr_list.json
```

### Backlog triage (themes)

```bash
usertest-backlog triage-backlog \
  --in apps/usertest_backlog/tests/fixtures/sample_issue_backlog.json
```

Optional flags:

- `--group-key <field>`: compute cross-group coverage using a specific field. If omitted,
  `package` is used automatically when present.
- `--out-json`, `--out-md`, `--out-xlsx`: override output paths. Defaults are based on the
  input filename (`.triage_backlog.json` and `.triage_backlog.md`).
- `--dedupe-overall-threshold`, `--theme-overall-threshold`, `--theme-k`,
  `--theme-representative-threshold`: tune clustering behavior.

Embedding/runtime notes:

- Real embedding runs require `OPENAI_API_KEY`.
- Set `TRIAGE_ENGINE_EMBED_CACHE_PATH` to reuse embeddings via an on-disk SQLite cache across
  repeated runs.
- XLSX output requires `openpyxl` to be installed in the environment.
```

---

## Configuration

This CLI relies on:

- run artifact contract (`docs/design/run-artifacts.md`)
- backlog policy and prompt manifests under `configs/`
- repo-local tracking under `.agents/` (plans, todos, actions ledgers; typically local-only / git-ignored)

Operational notes (security, CI guidance) live under `docs/ops/`.

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
cd apps/usertest_backlog
pdm run pytest -q tests/test_smoke.py
```
