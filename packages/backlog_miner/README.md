# `backlog_miner`

`backlog_miner` is a library for **LLM-assisted backlog mining**.

It is designed for workflows like:

- take a compiled dataset (`report.json` + evidence excerpts)
- run one or more “miner prompts” to propose issues/tickets
- optionally run labeler/merge passes
- output a structured backlog document for human review

It is used in `usertest-backlog` workflows but is intended for use with any set of
observations and issues.

---

## Install

Distribution name: `backlog_miner`

From this monorepo (editable):

```bash
pip install -e packages/backlog_miner
```

From a private GitLab PyPI registry (if you publish it):

```bash
pip install \
  --index-url "https://<gitlab-host>/api/v4/projects/<project_id>/packages/pypi/simple" \
  --extra-index-url "https://pypi.org/simple" \
  "backlog_miner==<version>"
```

> Publishing note
>
> This package is currently treated as **internal** unless opted into snapshot publishing via
> `[tool.monorepo].status` in `pyproject.toml`. See `docs/monorepo-packages.md`.

---

## Key concepts

### Prompt manifest

Backlog mining prompts are treated as data, not code.

- manifests describe which prompts exist and how to run them
- missing prompts should fail loudly (to avoid silent “fallback behavior”)

### Ensemble mining

You can run multiple prompts (or multiple models) and merge their outputs.
This is useful when you want:

- broader coverage
- cross-checking for hallucinations
- different “lenses” (UX vs security vs release engineering)

---

## Public API

Top-level exports:

- `load_prompt_manifest(path)`
- `run_backlog_prompt(...)`
- `run_backlog_ensemble(...)`
- `run_labeler_jobs(...)`
- `MinerJob`, `PromptManifest`

---

## How it fits in the system

`backlog_miner` runs after you have run artifacts.

Typical flow:

1) `usertest` produces run directories.
2) `usertest-backlog reports compile` builds a history file.
3) `backlog_miner` runs prompts over that history.
4) `backlog_core` renders backlog documents.

---

## Development

Run from the repo root:

```bash
python tools/scaffold/scaffold.py run install --project backlog_miner
python tools/scaffold/scaffold.py run test --project backlog_miner
python tools/scaffold/scaffold.py run lint --project backlog_miner
```
