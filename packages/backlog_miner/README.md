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

### Standalone package checkout (recommended first path)

Run from this package directory:

```bash
pdm install
pdm run smoke
pdm run test
pdm run lint
```

Dependencies for standalone use:
- `backlog_miner` imports `agent_adapters`, `backlog_core`, and `runner_core` at runtime.
- If your package index does not provide those internal packages, install local checkouts first.
- From a sibling checkout layout, add them as editable deps using `pdm` (paths relative to this
  package directory), for example:

```bash
pdm add -e ../agent_adapters -e ../backlog_core -e ../runner_core
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
- `load_pipeline_prompt_manifest(prompts_dir)` (six-stage pipeline manifest v2)
- `run_stage_prompt_json(...)` (generic stage prompt runner)
- `run_repro_research_stage(...)` (stage 3 repro+research runner)
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

## Six-stage backlog pipeline

The six-stage backlog pipeline treats each stage as an inspectable artifact boundary.

Key pieces:

- `configs/backlog_prompts/pipeline_manifest.json` (version 2) declares stage templates
- `backlog_miner.pipeline.load_pipeline_prompt_manifest(...)` validates templates + repo-owned
  guidance config
- `backlog_miner.pipeline.run_stage_prompt_json(...)` runs one stage prompt and persists prompt +
  response artifacts for auditability
- `backlog_miner.research_runner.run_repro_research_stage(...)` runs stage 3 in an isolated writable
  workspace via `runner_core.run_once(...)` and extracts a strict `extensions.backlog_repro_research`
  dossier block from the run report
- Stage 3 requires an explicit source ref, resolves local refs to a commit before acquisition, and
  independently replays each claimed experiment from a clean checkout. Evidence receipts bind the
  case and atom assignment, original artifacts, inspected baseline blobs and symbols, commands,
  assertions, outputs, and retained clean planning workspace. Downstream consumers revalidate those
  receipts before using the proof.
- Model-authored replay commands never run through a command shell. Production callers must supply
  an explicit replay executor; the backlog app selects a Docker image from
  `configs/backlog_research.yaml`, disables container networking, forwards only `CI=1`, receipts the
  actual sandbox metadata and image identity, and confirms container cleanup. Host execution is
  denied by default and requires an explicitly approved local source identity.
- Original and faithful research can replay a practical repository CLI or script, not only a test.
  The runner accepts the shell-free argv only when it resolves to immutable repository-owned code
  or config and either exactly matches the assigned source atom's retained command or names an
  entrypoint whose file was actually inspected. Absolute/traversal paths, URLs, interpreter code
  strings, PATH-only tools, and unbound model commands remain blocked. Runner-owned atom bindings
  retain the exact snapshot field path and value hash, including short wrong values and honestly
  contextual or corroborating atoms.

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
python tools/scaffold/scaffold.py run install --project backlog_miner
python tools/scaffold/scaffold.py run test --project backlog_miner
python tools/scaffold/scaffold.py run lint --project backlog_miner
```
