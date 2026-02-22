# agent_adapters

`agent-adapters` provides:
- thin wrappers for running Codex, Claude, and Gemini CLIs non-interactively
- raw-event normalizers that emit `normalized_events.jsonl`
- per-run MCP config rendering helpers (Codex)
- standalone fallback event helpers when `normalized_events` is not installed

This package is designed to be reusable outside this repo: if you can invoke an agent CLI headlessly and capture its tool log, you can normalize it into the shared event contract.

## Install

Distribution name: `agent-adapters`
Import package: `agent_adapters`

### Standalone package checkout (recommended first path)

Run from this package directory:

```bash
pdm install
pdm run smoke
pdm run test
pdm run lint
```

Notes:
- `normalized_events` integration is optional for this package; `agent_adapters.events` provides a
  built-in fallback implementation so standalone installs work in isolated environments.
- The `examples/mcp_with_sandbox_runner.md` flow additionally requires `sandbox_runner`.

If you need only a runtime install (without dev tooling commands), use:

```bash
python -m pip install -e .
```

From a private GitLab PyPI registry (snapshot publishing):

```bash
pip install \
  --index-url "https://<gitlab-host>/api/v4/projects/<project_id>/packages/pypi/simple" \
  --extra-index-url "https://pypi.org/simple" \
  "agent-adapters==<version>"
```

## Canonical smoke

```bash
pdm run smoke
pdm run smoke_extended
```

`pdm run smoke` is the deterministic first-success check. `pdm run smoke_extended` adds the
fixture-heavy normalization smoke pass.

---

## CLI

This package ships a small helper CLI:

```bash
agent-adapters --help
agent-adapters doctor
```

In most cases you will not call the adapter CLI directly; the `usertest` runner invokes adapters
under the hood.

## Core modules

- `agent_adapters.codex_cli` + `agent_adapters.codex_normalize`
- `agent_adapters.claude_cli` + `agent_adapters.claude_normalize`
- `agent_adapters.gemini_cli` + `agent_adapters.gemini_normalize`

## Normalized event shape

Normalizers emit a JSONL stream using shared event envelopes from `normalized_events`.
Common event `type` values:
- `agent_message`
- `run_command`
- `read_file`
- `write_file`
- `error`

Golden normalization fixtures are checked in under `examples/golden_runs/` for Codex, Claude,
and Gemini. Keep fixture outputs stable with:

`python -m pytest -q packages/agent_adapters/tests/test_golden_normalization_fixtures.py`

## MCP configuration (per run)

The `agent_adapters.mcp` package contains an agent-agnostic schema and Codex renderer for writing a
run-local `config.toml` without relying on global dotfiles.

See `examples/mcp_with_sandbox_runner.md`.

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
python tools/scaffold/scaffold.py run install --project agent_adapters
python tools/scaffold/scaffold.py run test --project agent_adapters
python tools/scaffold/scaffold.py run lint --project agent_adapters
```
