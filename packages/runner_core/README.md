# `runner_core`

This package contains the orchestration logic for a single agentic usertest run:

- Acquire a target repo into a workspace (`runner_core.target_acquire.acquire_target`)
- Resolve persona/mission/template/schema via the catalog (`runner_core.catalog`, `runner_core.run_spec`)
- Build the agent prompt via template substitution (`runner_core.prompt.build_prompt_from_template`)
- Invoke an agent adapter (Codex/Claude/Gemini via `agent_adapters`)
- Normalize raw tool logs into `normalized_events.jsonl`
- Compute metrics and render `report.md`

It is the core engine behind the `usertest` CLI, but can be used programmatically.

---

## Install

Distribution name: `runner_core`
Import package: `runner_core`

### Standalone package checkout (recommended first path)

Run from this package directory:

```bash
pdm install
pdm run smoke
pdm run test
pdm run lint
```

Dependencies for standalone use:
- `runner_core` imports sibling packages (`agent_adapters`, `normalized_events`, `reporter`, and `sandbox_runner`) at runtime.
- If your package index does not provide those internal packages, install local checkouts first.
- From a sibling checkout layout, run:

```bash
python -m pip install -e ../agent_adapters -e ../normalized_events -e ../reporter -e ../sandbox_runner
```

If you need only a runtime install (without dev tooling commands), use:

```bash
python -m pip install -e .
```

From a private GitLab PyPI registry (snapshot publishing):

```bash
pip install \
  --index-url "https://<gitlab-host>/api/v4/projects/<project_id>/packages/pypi/simple" \
  --extra-index-url "https://pypi.org/simple" \
  "runner_core==<version>"
```

Snapshot publishing status: `incubator` (see `docs/monorepo-packages.md`).

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

## Quickstart

The CLI is the primary interface, but the public API can be invoked directly:

```python
from pathlib import Path

from runner_core import RunnerConfig, RunRequest, run_once

cfg = RunnerConfig(
    repo_root=Path("/path/to/this/runner/repo"),
    runs_dir=Path("/path/to/this/runner/repo/runs/usertest"),
    agents={...},
    policies={...},
)

req = RunRequest(
    repo="/path/to/target/repo",
    agent="codex",
    policy="inspect",
    persona_id="quickstart_sprinter",
    mission_id="first_output_smoke",
)

result = run_once(cfg, req)
print(result.run_dir)
```

Most callers should use the CLI, because it loads `agents.yaml` / `policies.yaml` and handles
logging and run directory naming.

## Public API

- `RunnerConfig`, `RunRequest`, `RunResult`
- `run_once(config, request)`

## Notable behavior

- `USERS.md` is optional target context. If present, it is passed through to prompt templates that reference it.
- When edits are allowed, write activity is derived from `git diff --numstat` and recorded as:
  - `diff_numstat.json` (artifact)
  - `write_file` events appended to `normalized_events.jsonl` (with `lines_added` / `lines_removed`)

---

## Contracts

- Run directory layout: `docs/design/run-artifacts.md`
- Normalized events: `docs/design/event-model.md`
- Report schemas: `docs/design/report-schema.md`

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

Windows interpreter remediation:
- If smoke/preflight reports `windowsapps_alias`, disable the Microsoft Store app execution alias and install a full CPython distribution.
- If preflight reports `missing_stdlib` (for example, missing `encodings`), reinstall or repair Python and re-run `pdm run smoke`.
- Use `pwsh -File scripts/smoke.ps1 -SkipInstall` from monorepo root to print interpreter probe diagnostics before smoke tests.

### Monorepo contributor workflow

Run from the monorepo root:

- Run tests: `python tools/scaffold/scaffold.py run test --project runner_core`
- Run lint: `python tools/scaffold/scaffold.py run lint --project runner_core`
