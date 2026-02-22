# CLI reference

This repo ships three end-user CLIs:

- `usertest` (app: `apps/usertest`) – run usertests and render run artifacts
- `usertest-backlog` (app: `apps/usertest_backlog`) – compile/analyze/export run history and triage PRs
- `usertest-implement` (app: `apps/usertest_implement`) – implement one exported backlog ticket in a target repo

If you’re unsure where to start, read `docs/tutorials/getting-started.md`.

---

## `usertest`

Entry points:

- `usertest …` (installed script)
- `python -m usertest.cli …` (module invocation)

### Core commands

- `usertest run`
  - Run a single target repo.
  - Writes a run directory under `runs/usertest/…`.
- `usertest batch`
  - Run multiple targets from a YAML file.
- `usertest report`
  - Re-render `report.md` / `report.json` for an existing run directory.

### Discovery commands

- `usertest personas list`
- `usertest missions list`

These reflect the merged catalog (runner defaults + target `.usertest/catalog.yaml` if present).

### Scaffolding command

- `usertest init-usertest`
  - Initialize `.usertest/` inside a *local* target repo.
  - Produces a starter `catalog.yaml` and optional sandbox install manifest.

### Reports pipeline

- `usertest reports compile`
  - Compile run directories into a JSONL history.
- `usertest reports analyze`
  - Analyze a history file and produce an issue summary.

Most backlog/ticket workflows have moved to `usertest-backlog`.

---

## `usertest-backlog`

Entry points:

- `usertest-backlog …` (installed script)
- `python -m usertest_backlog.cli …` (module invocation)

### Reports workflows

Commands are grouped under `usertest-backlog reports`:

- `compile` – build a run history file
- `analyze` – analyze outcomes
- `intent-snapshot` – snapshot a repo intent for analysis
- `review-ux` – UX-focused review of reports
- `export-tickets` – export tickets (format depends on configured exporter)
- `backlog` – build/render backlog documents

### PR triage

- `usertest-backlog triage-prs`

---

## `usertest-implement`

Entry points:

- `usertest-implement …` (installed script)
- `python -m usertest_implement.cli …` (module invocation)

### Core command

- `usertest-implement run`
  - Implement a single exported backlog ticket in a target repo.
  - Writes a run directory under `runs/usertest_implement/…` with ticket linkage artifacts.
  - Optional git finalization:
    - `--commit` creates a branch + commit in the kept workspace.
    - `--push` pushes the branch to the configured remote.
    - `--pr` attempts best-effort PR creation using GitHub CLI (`gh`). (`gh` must be on `PATH` and authenticated.)

### Reports utilities

- `usertest-implement reports summarize`
  - Summarize implementation runs into JSONL for analysis.

### Ticket queue helpers

- `usertest-implement tickets list|next|move`
  - Work with `.agents/plans/*` ticket queues.
- `usertest-implement tickets run-next`
  - Standard flow: refresh backlog exports (including `review-ux`) and implement the next ticket (research-first).

---

## Common flags and concepts

### `--repo-root`

Path to *this* runner repo’s root. Used to locate `configs/`, prompt templates, schemas, etc.

### `--repo`

The target under test. Can be:

- local path
- git URL
- `pip:<package>` / `pdm:<spec>` for “fresh install” evaluations

### `--agent`

Which adapter to use (`codex`, `claude`, `gemini`). Configured in `configs/agents.yaml`.

### `--policy`

Execution policy for the agent (`safe`, `inspect`, `write`). Configured in `configs/policies.yaml`.

---

## Always prefer `--help` for exact flags

These CLIs evolve quickly.

Use:

```bash
python -m usertest.cli --help
python -m usertest.cli run --help
python -m usertest_backlog.cli --help
python -m usertest_implement.cli --help

# If you installed the console scripts:
usertest --help
usertest-backlog --help
usertest-implement --help
```
