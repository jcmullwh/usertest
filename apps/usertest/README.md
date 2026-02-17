# `usertest` CLI

`usertest` is the **end-user CLI** for running “agent usertests” against a target repository.

It orchestrates:

- target acquisition (local path / git URL / `pip:` targets)
- prompt assembly (persona + mission + policy)
- agent invocation (Codex / Claude Code / Gemini)
- artifact capture + normalization
- report + metrics rendering

If you’re looking for the fastest on-ramp, start at `docs/tutorials/getting-started.md`.

---

## Install

From the monorepo root (editable install):

```bash
python -m pip install -r requirements-dev.txt
python -m pip install -e apps/usertest
```

Confirm:

```bash
usertest --help
```

> Naming note
>
> In `tools/scaffold/monorepo.toml`, this app’s project id is `cli`.
> In `pyproject.toml`, the distribution name is also `cli`.
> The installed command is **still** `usertest`.

---

## Core commands

### `usertest run`

Run a single target and write a run directory under `runs/usertest/…`.

```bash
usertest run \
  --repo-root . \
  --repo "PATH_OR_GIT_URL" \
  --agent codex \
  --policy inspect
```

### `usertest batch`

Run multiple targets from a YAML file.

```bash
usertest batch --repo-root . --targets examples/targets.yaml --agent codex --policy safe
```

### `usertest report`

Re-render `report.md` (and optionally recompute metrics) for an existing run directory:

```bash
usertest report --repo-root . --run-dir "RUN_DIR" --recompute-metrics
```

### `usertest init-usertest`

Initialize `.usertest/` inside a **local** target repo:

```bash
usertest init-usertest --repo-root . --repo "PATH/TO/TARGET"
```

This is the recommended way to store repo-specific personas/missions in the repo itself.

### `usertest reports …`

Post-process existing runs into compiled artifacts:

- `compile`: build a JSONL report history
- `analyze`: summarize outcomes and write an issue analysis

Most backlog mining/review/export workflows live in `usertest-backlog`.

---

## Configuration

This CLI loads configuration from the runner repo:

- `configs/agents.yaml` (how to invoke agent CLIs)
- `configs/policies.yaml` (safe/inspect/write)
- `configs/catalog.yaml` (persona/mission/template discovery)

Target repos can extend/override the catalog via `.usertest/catalog.yaml`.

Reference: `docs/reference/configuration.md`.

---

## Output

Runs are written under:

`runs/usertest/<target>/<timestamp>/<agent>/<seed>/`

The run directory contains rich evidence logs. Treat it as sensitive by default.

Contract: `docs/design/run-artifacts.md`.

---

## Development

From the repo root:

```bash
python tools/scaffold/scaffold.py run install --project cli
python tools/scaffold/scaffold.py run test --project cli
python tools/scaffold/scaffold.py run lint --project cli
```

Smoke tests:

```bash
python -m pytest -q apps/usertest/tests/test_smoke.py apps/usertest/tests/test_golden_fixture.py
```
